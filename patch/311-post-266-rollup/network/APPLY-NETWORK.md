# Patch 311 — Network layer: Secrets Manager VPCE (#309)

This replaces the old `cdk/` directory. The VPCE change lived in `observability.py`, but
**`cdk deploy` is forbidden on this deployment** (it was CDK-deployed once and then
manually modified; a deploy would overwrite those manual changes). So the endpoint is
handled by hand — and because this is a **network** resource, the rule is strict:

> **AI is DESCRIBE-ONLY on network resources.** Do not create, modify, or delete any VPC
> endpoint, route, DNS/private-DNS, subnet, NAT, or security group. Run only the `describe`
> probes below, present the impact assessment and the exact proposed command, then STOP.
> A human (the Claude Code terminal user) must explicitly approve before any create/modify
> runs. A wrong VPCE/DNS change can break resolution for the whole VPC.

## What this fixes (and when you even need it)

`observability.py` adds a Secrets Manager Interface VPCE so the AOS rolesmapping Lambda
(in-VPC) can reach `secretsmanager.<region>.amazonaws.com` without NAT. On an imported
customer VPC with no NAT, that call times out and the AOS bootstrap fails (#295/#309).

**You may not need it at all.** Decide by live probe, not by assumption:

- If the observability / AOS logging domain is NOT deployed → there is no rolesmapping
  Lambda → **skip this whole layer.**

## Permissions needed for the probes (read-only)

`ec2:DescribeVpcEndpoints`, `ec2:DescribeRouteTables`, `ec2:DescribeSubnets`,
`ec2:DescribeSecurityGroups`, `lambda:GetFunctionConfiguration`. All read-only — the
describe phase needs no write permission. The create phase (human-approved) additionally
needs `ec2:CreateVpcEndpoint`, `ec2:CreateSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`.

## Step 1 — Probe: is there already a usable endpoint? (one-vote veto)

```bash
aws ec2 describe-vpc-endpoints --region <region> \
  --filters "Name=service-name,Values=com.amazonaws.<region>.secretsmanager" \
            "Name=vpc-id,Values=<vpc-id>" \
  --query 'VpcEndpoints[?VpcEndpointType==`Interface` && State==`available` && PrivateDnsEnabled==`true`].[VpcEndpointId,SubnetIds,Groups]' \
  --output json
```

Only reusable if: Interface + available + private-DNS on + its SG allows 443 from the
Lambda SG + it covers the Lambda's subnets/AZs. If a usable one exists → **reuse it, create
nothing** (AWS allows only ONE private-DNS endpoint per service per VPC — a second create
conflicts). This is the `create_secretsmanager_vpce: false` case from `config.yml`.

## Step 2 — No usable endpoint: does the Lambda actually reach the API?

Resolve the Lambda's real subnets, then check each subnet's effective route table for an
ACTIVE default route via NAT:

```bash
aws lambda get-function-configuration --function-name <AosRolesMapFn> --region <region> \
  --query 'VpcConfig.[SubnetIds,SecurityGroupIds]' --output json

# per subnet — explicit association:
aws ec2 describe-route-tables --region <region> \
  --filters "Name=association.subnet-id,Values=<lambda-subnet>" \
  --query 'RouteTables[].Routes[?DestinationCidrBlock==`0.0.0.0/0` && NatGatewayId!=`null` && State==`active`]' \
  --output json
# if empty, the subnet may use the VPC main route table:
aws ec2 describe-route-tables --region <region> \
  --filters "Name=vpc-id,Values=<vpc-id>" "Name=association.main,Values=true" \
  --query 'RouteTables[].Routes[?DestinationCidrBlock==`0.0.0.0/0` && NatGatewayId!=`null`]' --output json
```

- Active NAT default route on every Lambda subnet → Lambda can reach Secrets Manager →
  **no VPCE needed.** (A public subnet does NOT help: a VPC-attached Lambda ENI has no
  public IP, so an IGW route gives no egress — only NAT or the VPCE does.)
- No NAT → go to Step 3.

**network.mode as expectation only (never the decision):** `default_vpc`/`self_managed`
usually have egress → no VPCE; `imported` (customer VPC) often lacks NAT → likely needs it.
The decision always comes from the probes above.

## Step 3 — Propose the VPCE (DO NOT run it — human approves)

Present this to the terminal user with the Step 1/2 findings and the impact
("creates an Interface endpoint + SG in <vpc>, enables private DNS for
secretsmanager.<region>.amazonaws.com"). Only run after explicit approval:

```bash
# SG: 443 from the Lambda SG only
aws ec2 create-security-group --group-name openclaw-sm-vpce-311 \
  --description "SM VPCE 443 from AOS rolesmapping Lambda" --vpc-id <vpc-id> --region <region>
aws ec2 authorize-security-group-ingress --group-id <new-sg> \
  --protocol tcp --port 443 --source-group <lambda-sg> --region <region>
# the endpoint
aws ec2 create-vpc-endpoint --vpc-endpoint-type Interface --region <region> \
  --vpc-id <vpc-id> --service-name com.amazonaws.<region>.secretsmanager \
  --subnet-ids <lambda-subnets> --security-group-ids <new-sg> --private-dns-enabled
```

## Rollback

```bash
aws ec2 delete-vpc-endpoints --vpc-endpoint-ids <id> --region <region>
aws ec2 delete-security-group --group-id <new-sg> --region <region>
```

## Verify

The rolesmapping Lambda's next invocation reaches Secrets Manager (no connect timeout in
its logs); private DNS resolves `secretsmanager.<region>.amazonaws.com` to the endpoint.
