"""Helper functions for CDK stack (shared across domain modules, issue #87)."""
import hashlib as _hashlib
import json as _json
import platform as _platform
from pathlib import Path
import jsii as _jsii
from aws_cdk import (
    ArnFormat,
    Aspects,
    CustomResource,
    Duration,
    IAspect,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as _lambda,
    custom_resources as cr,
)

# 为什么要自写 handler 而不用 AwsCustomResource:CFN 建完 CR 的 IAM policy 会【立刻】
# 调 Lambda,而 IAM 传播需要时间 —— 真机实测首次调用必 AccessDenied(报 "User ... is not
# authorized to perform: autoscaling:UpdateAutoScalingGroup"),8~17s 后才生效,必须重试。
# AwsCustomResource 没有重试开关,它的 ignore_error_codes_matching 只会吞掉错误返回
# SUCCESS(ASG 仍是数字版本 → promote 永久 409),属假绿,不能用。
#
# 注意区分两种 AccessDenied 文案,只有前者是等就能好的瞬态:
#   "User ... not authorized to perform: autoscaling:UpdateAutoScalingGroup" = 策略未传播
#   "You are not authorized to use launch template: lt-xxx"                  = 权限真缺
# 后者等多久都不会好(实测重试 82s 全失败),真因是缺 ec2:CreateTags(见下方策略注释)。
#
# 重试的粒度是【整轮 Describe + Update + 回读】而不只是 Update(``_converge``):
#   - Describe 用的 autoscaling:DescribeAutoScalingGroups 就在这条正在传播的策略里,
#     放在重试外会让首次调用直接 fail-loud、整栈回滚 —— 而重试存在的理由正是它。
#   - 回读受 ASG 读路径最终一致影响(刚写完可能仍读到旧值,本次真机证明脚本也要 sleep
#     才稳),放在重试外会把"已成功"判成失败。重发 update 幂等(同值同属性,不触发实例替换)。
# ``_transient`` 优先看结构化 AWS 错误码,拿不到才退回文案:AccessDenied 系是 IAM 传播,
# ScalingActivityInProgress/ResourceContention/Throttling 系是 ASG 侧瞬态,``_READBACK``
# 前缀是回读未收敛。"not authorized to use launch template" 归到重试侧只是多等一轮再
# fail-loud(不会误报成功),而漏判真瞬态会直接整栈回滚,故按此侧倾斜。
#
# ``_converge`` 写回的 ``LaunchTemplateSpecification`` 【只能】带 ``LaunchTemplateId``
# 和 ``Version``,不能把 Describe 回来的字段原样带上:
#   - 该结构总共只有 Id / Name / Version 三个字段(官方 API 参考),而 Describe 回来
#     的 spec 【总是】同时带 Id 和 Name(真机 Describe 实测,host/edge/探针 ASG 全带)。
#   - 同时给 Id 和 Name 会被拒:真机 A/B 实测 —— 带两个报
#     ``ValidationError: Valid requests must contain either launchTemplateId or
#     LaunchTemplateName``(文案读着像"至少给一个",实际是"只能给一个"),只给 Id 成功
#     且 Describe 回来 ASG 自己补上 Name。所以"保留其它字段"这种写法在真机上必炸,
#     且 ``_transient`` 判 ValidationError 为非瞬态 → 立刻整栈 ROLLBACK。
#   - MIP 形态只替换嵌套的那个 spec,``Overrides`` / ``InstancesDistribution`` 留在
#     外层 mip dict 上原样回写:它们一旦变化,ASG 会判定需要替换实例 —— 那会终止跑在
#     host 上的租户 microVM(no-data-loss 红线)。
#
# 写之前先校验现有 ``LaunchTemplateId`` 与 CR 属性一致:CR 只被授权管自己那一个 ASG +
# 自己那一个 LT。如果这个 ASG 现在用的是别的 LT(人为改配、ASG 名被复用、fleet 迁移),
# 无条件写回本 CR 的 lt_id 会把它拉到另一个 fleet 的模板上,属跨 fleet 越权写。此时必须
# fail-loud 让整栈回滚,而不是"顺手纠正"。``got_id`` 为空(极少见的只给 Name 的老 ASG)
# 才允许补写 id。
#
# 内联 handler 受 CFN ZipFile 4096 字符硬上限约束,长解释一律留在本模块注释里(不占额度)。
_TRACK_DEFAULT_HANDLER = '''\
import time
import boto3

asg = boto3.client("autoscaling")

# 回读不符的自造错误前缀,_transient 靠它把"刚写完还没读到"判为瞬态。
_READBACK = "readback-not-yet"


def _spec_key(name):
    """判断该 ASG 用的是裸 LaunchTemplate 还是 MixedInstancesPolicy。

    MIP 与 LaunchTemplate 属性互斥(ha_edge.py 在 instance pool >= 2 时把
    LaunchTemplate 置 None 改用 MIP),改错那个 key 会被 ASG API 拒绝或静默无效。
    """
    g = (asg.describe_auto_scaling_groups(AutoScalingGroupNames=[name])
         .get("AutoScalingGroups") or [])
    if not g:
        raise RuntimeError("ASG %s not found" % name)
    g = g[0]
    if g.get("MixedInstancesPolicy"):
        return "mip", g
    if g.get("LaunchTemplate"):
        return "lt", g
    raise RuntimeError("ASG %s has neither LaunchTemplate nor MixedInstancesPolicy" % name)


def _version_of(kind, g):
    if kind == "mip":
        return ((g["MixedInstancesPolicy"].get("LaunchTemplate") or {})
                .get("LaunchTemplateSpecification") or {}).get("Version")
    return (g.get("LaunchTemplate") or {}).get("Version")


def _transient(e):
    """等一下会好的错误。判据与倾斜理由见 _helpers.py 模块顶部注释。"""
    code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                "Throttling", "ThrottlingException", "RequestLimitExceeded",
                "ScalingActivityInProgress", "ResourceContention",
                "ResourceContentionFault"):
        return True
    s = str(e)
    return ("AccessDenied" in s or "not authorized" in s
            or s.startswith(_READBACK))


def _converge(name, lt_id):
    """一轮收敛:Describe -> 只改 Version -> 回读。理由见模块顶部注释。"""
    kind, g = _spec_key(name)
    got_id = (g["MixedInstancesPolicy"]["LaunchTemplate"]["LaunchTemplateSpecification"]
              if kind == "mip" else g["LaunchTemplate"]).get("LaunchTemplateId")
    if got_id and got_id != lt_id:
        raise RuntimeError("ASG %s 用 LT %s,非本 CR 的 %s;拒写" % (name, got_id, lt_id))
    # 只能 Id + Version:同时带 Describe 回来的 Name 会被 ASG API 拒(见模块顶部注释)。
    spec = {"LaunchTemplateId": lt_id, "Version": "$Default"}
    if kind == "mip":
        mip = g["MixedInstancesPolicy"]
        mip["LaunchTemplate"]["LaunchTemplateSpecification"] = spec
        asg.update_auto_scaling_group(
            AutoScalingGroupName=name, MixedInstancesPolicy=mip)
    else:
        asg.update_auto_scaling_group(AutoScalingGroupName=name, LaunchTemplate=spec)
    # 必须回读:不回读就报 SUCCESS 属假绿(bootstrap_version_service 硬校验该值,
    # 存成数字会让 promote 永久 409)。
    got = _version_of(*_spec_key(name))
    if got != "$Default":
        raise RuntimeError("%s:%s 的 Version 是 %r,不是 $Default" % (
            _READBACK, name, got))
    return got


def on_event(event, ctx):
    rt = event.get("RequestType")
    p = event["ResourceProperties"]
    name, lt_id = p["AsgName"], p["LaunchTemplateId"]
    pid = event.get("PhysicalResourceId") or ("track-default-" + name)
    if rt == "Delete":
        # 删除 no-op:栈销毁时 ASG 本身也在删,改它的 Version 无意义且会引竞态。
        return {"PhysicalResourceId": pid}
    err = None
    for i in range(8):  # IAM 传播实测约 14s 生效;退避总上限约 75s
        try:
            return {"PhysicalResourceId": pid, "Data": {"Version": _converge(name, lt_id)}}
        except Exception as e:  # noqa: BLE001
            if not _transient(e):
                raise  # 非瞬态(如 ASG 不存在、参数错)立刻 fail-loud,不靠重试掩盖
            err = e
            time.sleep(min(2 ** i, 15))
    raise RuntimeError("%s 重试后仍未收敛到 $Default: %s" % (name, err))
'''


def _no_repr_fallback(value):
    """指纹里绝不允许退回 ``repr``(#389 块5)。

    jsii proxy 的 ``repr`` 带内存地址(``<...object at 0x105222cf0>``),每次 synth 都不同,
    会让 ``AsgShape`` 每次部署都变 —— ``cdk diff`` 永久脏、tracker 每次白跑一遍。宁可
    synth 期炸出来,也不能悄悄产出不稳定的指纹。
    """
    raise TypeError(
        "AsgShape 指纹遇到不可 JSON 序列化的 %s —— 不能退回 repr(带内存地址会让指纹"
        "每次 synth 都变)。先把它 resolve 成纯 JSON 再进指纹。" % type(value).__name__
    )


def _asg_shape_fingerprint(scope, asg, extra):
    """ASG 声明形态的指纹,进 CR 属性用来"CFN 动了 ASG 就重跑 tracker"(#389 块5)。

    为什么必须有:真机探针实测(零实例探针栈 ``oc389-cfn-reset-probe``),只要 ASG 资源
    进了变更集,CFN 就会连带下发 ``LaunchTemplateSpecification.Version``,把 tracker
    写好的 ``$Default`` 覆盖成模板里的数字 —— 即使模板里那个 Version 表达式本身没变
    (T1 只改 ``MaxSize``,裸 LT 与 MIP 两种形态都被拉回数字 1;T2 的
    ``UPDATE_ROLLBACK`` 同样重写)。覆盖后 promote 永久 409 ``ASG_NOT_TRACKING_DEFAULT``。
    第二轮探针(``oc389-crp2``,带真 tracker CR)证明:CR 属性变过,更新与回滚两条路径
    都会重跑 CR 并收敛回 ``$Default``(T1'/T2'/T3' 7/7 PASS)。

    所以触发条件必须与"ASG 进变更集"【等价】,也就是覆盖它的整个声明面,而不是挑几个
    属性:挑清单等于漏防 —— 改了没进清单的属性(``TerminationPolicies`` /
    ``CapacityRebalance`` / ``DefaultInstanceWarmup`` / ``InstanceMaintenancePolicy`` /
    ``MetricsCollection`` / ``NotificationConfigurations`` / ``MaxInstanceLifetime`` /
    ``NewInstancesProtectedFromScaleIn`` / ``ServiceLinkedRoleARN`` / ``Tags``)时
    ASG 照样进变更集、Version 照样被写回数字,但指纹不变 → tracker 不重跑 → 部署假绿。

    故取 L1 的完整属性集 ``_cfn_properties``,而不是逐个读公开 getter。用私有名的理由和
    边界(本地 aws-cdk-lib 2.255.0 实测):
      - 它是 ``CfnResource`` 基类成员,``Stack.resolve`` 后是纯 JSON(``{"Ref": ...}`` /
        ``Fn::GetAtt`` 结构原样进哈希),同一份代码重复 synth 逐字稳定;
      - 只含【已声明】的属性,没设的不出现,所以不会因 CDK 补默认值而漂;
      - 上面那 10 个属性逐个改动都能触发指纹变化(旧的 8 属性写法逐个都漏),Tags 走
        ``TagManager`` 也在内;
      - 万一将来 CDK 去掉这个名字,``AttributeError`` 会在 synth 期直接炸 —— 是 fail-loud,
        不是静默退化成部分覆盖。``_render_properties`` 不能用(实测对 ASG 抛
        "Supplied properties not correct");``Lazy`` 里再 ``resolve`` 也不能用(实测
        resolve-in-resolve 报 "Converting circular structure to JSON")。

    ``updatePolicy`` 一并进指纹:它是资源级 attribute 而不是 Properties,改它(如滚动更新
    批次)同样让 ASG 进变更集,``_cfn_properties`` 覆盖不到。

    ``extra`` 仍必传调用方用 ``add_property_override`` 写进去的那部分:override 存在
    L1 的 raw overrides 里,既不在 ``_cfn_properties`` 也没有公开读口(实测本地
    aws-cdk-lib 无 ``raw_overrides``),漏传会让那部分变化不触发重跑。

    本函数由 ``_StampAsgShape`` 在 synth 阶段调用,不在 construct 期定值 —— 理由见该类。
    """
    shape = Stack.of(scope).resolve(
        {
            "props": asg._cfn_properties,
            "updatePolicy": asg.cfn_options.update_policy,
            "extra": extra,
        }
    )
    return _hashlib.sha256(
        _json.dumps(shape, sort_keys=True, default=_no_repr_fallback).encode()
    ).hexdigest()[:32]


@_jsii.implements(IAspect)
class _StampAsgShape:
    """synth 末尾才把 ``AsgShape`` 指纹盖进 tracker 的 CR 属性(#389 块5)。

    为什么不能在 ``track_default_lt_version()` 里直接算完写死:那是 construct 期,之后
    任何对 ASG 的改动都不会进指纹,于是形成一个静默顺序陷阱 —— 那次部署 ASG 进变更集、
    Version 被 CFN 写回数字,而指纹还是老值,tracker 不重跑,部署假绿。本仓库真的有这条
    路径:``outputs.py`` 在所有 domain 模块跑完之后统一 ``override_logical_id`` 钉 ASG /
    LT 的 logical id,指纹里的 ``Ref`` / ``Fn::GetAtt`` 因此会变(本地 synth 实测)。

    Aspect 在 ``synth`` 阶段遍历,此时全栈声明已定稿,所以指纹与"CFN 看到的 ASG"一致,
    顺序依赖从根上消失。``add_property_override`` 是 CR L1 上的原样覆盖,不受 construct
    期属性已固化影响。

    实现细节:靠 ``node.node.path`` 认目标,不能用 ``node is <l1>`` —— jsii proxy 每次
    穿越边界是不同 Python 对象,身份比较恒为 False(本地实测:Aspect 确实访问到了该
    节点,但 ``is`` 判定不成立,导致指纹停在占位符)。
    """

    def __init__(self, cr_l1, recompute):
        self._path = cr_l1.node.path
        self._cr_l1 = cr_l1
        self._recompute = recompute

    def visit(self, node):
        if node.node.path == self._path:
            self._cr_l1.add_property_override("AsgShape", self._recompute())


def _lt_sourced_image_arn(scope):
    """必须来自 LaunchTemplate 而不能由请求覆盖的资源:AMI(#389 块5)。

    AMI ARN 不带 account 段(``arn:aws:ec2:<region>::image/*``),故 account 显式置空。
    """
    return scope.format_arn(
        service="ec2", resource="image", resource_name="*", account=""
    )


def track_default_lt_version(
    scope,
    node_id,
    asg,
    lt,
    instance_role,
    asg_shape=None,
    ssm_image_parameter=None,
):
    """让 ASG 跟踪 LaunchTemplate 的 ``$Default`` 版本(#389 块5)。

    CloudFormation 【不能】把 ``$Default`` 写进 ASG:``LaunchTemplateSpecification.Version``
    是必填项且 CFN 在 resource handler 阶段硬拒该字面值
    ("CloudFormation does not support using $Latest or $Default for LaunchTemplate
    version") —— 真机实测,与生产 EdgeASG UPDATE_FAILED 报错逐字一致。
    ``Fn::GetAtt DefaultVersionNumber`` 能过 CFN,但它在部署期就固化成数字,而
    ``bootstrap_version_service._resolve_lt_id`` 硬要求 Version 字面等于 ``$Default``
    (否则 409 ASG_NOT_TRACKING_DEFAULT,因为翻默认版本对固定版本的 ASG 无效),所以
    GetAtt 会让 promote 永久失效。

    唯一出路:CFN 模板给数字版本建 ASG(过校验),部署后由本 CR 调 ASG API 改成 ``$Default``
    —— ASG API 层接受并原样保存该字面值,且翻 LT 默认版本后仍是 ``$Default``(是引用而非
    快照)。drift 检测能看到 ``MODIFIED``,非静默漂移。以上均真机实测。

    但 ``$Default`` 会被 CFN 重新覆盖:只要 ASG 资源进了变更集,CFN 就连带下发
    ``Version``,把它写回模板里的数字 —— 即使那个表达式本身没变(真机探针 T1 只改
    ``MaxSize``,裸 LT 与 MIP 两种形态都中;T2 的 ``UPDATE_ROLLBACK`` 同样重写)。
    所以 CR 属性里带 ``AsgShape`` 指纹,让"CFN 动了 ASG"必然带出属性变化触发重跑,
    详见 ``_asg_shape_fingerprint``。

    权限:ASG 改 LT 版本时会拿调用者身份做一次真实的 ``RunInstances`` dry run 预校验
    (官方 ec2-auto-scaling-launch-template-permissions 明写),所以这四项都是必需的,
    一项不给就报 ``You are not authorized to use launch template: lt-xxx``,且该文案
    【不点名】缺哪个 action。真机逐层剥权实测(零实例临时 ASG,每次真实值变更):
    只给 autoscaling / +RunInstances / +RunInstances+CreateTags 三种都被拒,仅全集通过。

    RunInstances 按官方 ``NotResource`` 范式拆两条收窄(见下方语句注释):AMI 必须来自
    LT,其余资源允许来自请求(ASG 的 subnet 来自 VPCZoneIdentifier,不在 LT 里),
    故请求里换 ImageId 会被拒。PassRole 锁到该 LT 的实例角色。

    还有第五项 ``ssm:GetParameters``,只在 ``ssm_image_parameter`` 非空时加:
    ``host.golden_ami.use=true`` 时 LT 的 ImageId 是 ``resolve:ssm:<param>`` 占位符,
    上面那次预校验会真的去解析它。真机 A/B 实测(零实例 ASG):不给这项被拒且 ASG 停在
    数字版本,补上立刻收敛到 ``$Default``。所以 golden AMI 一开,不给这项 tracker 必失败
    → 整栈 ROLLBACK。edge 用官方 AL2023 AMI(不走 ``resolve:ssm``),传 None 就不授。

    用 ``cr.Provider`` + 自写 handler(house style 同 observability.py/image.py)而不是
    ``AwsCustomResource``:见 ``_TRACK_DEFAULT_HANDLER`` 顶部注释 —— IAM 传播竞态需要重试,
    而 AwsCustomResource 只能吞错(假绿)。专属角色也避免污染全栈共享的 CR 单例 Lambda。

    ResourceProperties 里两个值都只为触发 Update(真机探针 ``oc389-crp2`` 证明属性变过
    时更新与回滚两条路径都会重跑 CR 并收敛回 ``$Default``):

    - ``LtVersion``(= LT 的 LatestVersionNumber):LT 内容变更的部署带出新版本号。
    - ``AsgShape``:ASG 声明形态指纹,覆盖"LT 没变但 CFN 改了 ASG"这一路(如只调
      ``host_count``/容量),否则那次部署会静默留下数字版本。见
      ``_asg_shape_fingerprint``。

    仍有一个边界:两者都没变时 CFN 判定 no changes、CR 不重跑,此时人为改出的漂移
    【不会】被这次 deploy 纠正(真机实测确认)。这不影响 promote —— promote 自己会发
    新版本并翻默认,且它执行前硬校验 Version 必须是 ``$Default``,漂移只会让它 409 拒绝
    而不会误操作;要纠正漂移就改 LT 或直接调 ASG API。
    """
    fn = _lambda.Function(
        scope,
        f"{node_id}Fn",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="index.on_event",
        timeout=Duration.minutes(3),  # 8 次退避重试上限约 75s,留足余量
        code=_lambda.Code.from_inline(_TRACK_DEFAULT_HANDLER),
    )
    lt_arn = scope.format_arn(
        service="ec2",
        resource="launch-template",
        resource_name=lt.launch_template_id,
    )
    for st in (
        iam.PolicyStatement(
            actions=["autoscaling:UpdateAutoScalingGroup"],
            # ASG ARN 的 group-id 段在建栈时未知,用 * 占位;组名段锁死到本 ASG。
            # 必须 COLON_RESOURCE_NAME:ASG ARN 的形态是
            # `...:autoScalingGroup:<uuid>:autoScalingGroupName/<name>`(冒号分隔),
            # format_arn 默认的 `/` 分隔会产出 `autoScalingGroup/*:...`,真机 AccessDenied。
            resources=[
                scope.format_arn(
                    service="autoscaling",
                    resource="autoScalingGroup",
                    arn_format=ArnFormat.COLON_RESOURCE_NAME,
                    resource_name=(
                        f"*:autoScalingGroupName/{asg.auto_scaling_group_name}"
                    ),
                )
            ],
        ),
        iam.PolicyStatement(
            actions=["autoscaling:DescribeAutoScalingGroups"], resources=["*"]
        ),
        # RunInstances 拆两条(官方 ExamplePolicies_EC2「Launch templates」第三个范式)。
        # 只写 Resource="*" + ArnEquals LT 不够:官方明写该形态下 "users can override any
        # parameters in the launch template by specifying the parameters in the
        # RunInstances action" —— 持有者能在请求里换 ImageId 起任意镜像,配下面的
        # PassRole 就等于拿到 host 实例角色。叠 ``ec2:IsLaunchTemplateResource=true``
        # 可堵住,但【不能一把盖到 "*" 上】:ASG 的 subnet 来自 VPCZoneIdentifier 而非 LT,
        # 该条件对它求值 false,正常调用会连带被拒(真机实测 UnauthorizedOperation,
        # 报 subnet/subnet-xxx 未授权 → tracker CR 失败 → 整栈 ROLLBACK)。
        # 故按官方 NotResource 范式只把 image 拆出来单独加该条件:AMI 必须来自 LT,
        # 其余资源(subnet/eni/volume/sg...)照旧允许来自请求。真机 A/B 实测该形态:
        # 正常 dry run 通过、``--image-id`` 覆盖被拒(UnauthorizedOperation)。
        #
        # 已知残余:``ec2:IsLaunchTemplateResource`` 官方定义只 "prevent users from
        # overriding any pre-existing ARNs in the launch template",UserData 不是 ARN
        # 资源,所以覆盖 UserData 在 IAM 层拦不住(真机实测确认)。这不是本形态的缺陷,
        # 而是该条件键的能力边界 —— 兜底靠"该权限只属于这个 CR 专属 Lambda 角色、
        # 代码里从不调 RunInstances"这一层,不靠 IAM。
        iam.PolicyStatement(
            actions=["ec2:RunInstances"],
            not_resources=[_lt_sourced_image_arn(scope)],
            conditions={"ArnEquals": {"ec2:LaunchTemplate": lt_arn}},
        ),
        iam.PolicyStatement(
            actions=["ec2:RunInstances"],
            resources=[_lt_sourced_image_arn(scope)],
            conditions={
                "ArnEquals": {"ec2:LaunchTemplate": lt_arn},
                "Bool": {"ec2:IsLaunchTemplateResource": "true"},
            },
        ),
        # LT 内嵌 TagSpecifications(host/edge LT 都带 Project/Role 标签)时,上面那次
        # RunInstances 预校验会连带校验 CreateTags —— 缺它一样只报
        # "not authorized to use launch template",不点名缺哪个 action(真机 A/B 实测:
        # 无 tag 的 LT 不需要它、带 tag 的 LT 缺它必 AccessDenied、只加它即通过)。
        # CreateAction 条件把范围限死在"起实例时打标签",拿不到给存量资源改标签的能力。
        iam.PolicyStatement(
            actions=["ec2:CreateTags"],
            resources=["*"],
            conditions={"StringEquals": {"ec2:CreateAction": "RunInstances"}},
        ),
    ):
        fn.add_to_role_policy(st)
    if instance_role is not None:
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"], resources=[instance_role.role_arn]
            )
        )
    # golden_ami.use=true 时 host LT 的 ImageId 是 `resolve:ssm:<param>` 占位符,
    # 上面那次 RunInstances 预校验会【真的去解析它】,所以还要 ssm:GetParameters。
    # 缺它真机报 ValidationError "You must use a valid fully-formed launch template.
    # User ... is not authorized to perform: ssm:GetParameters on resource ..."
    # → tracker 失败 → 整栈 ROLLBACK(真机 A/B 实测:五条策略被拒且 ASG 停在数字版本,
    # 补这一条后立刻收敛到 $Default;3/3)。注意这条文案【点名】缺哪个 action,
    # 与 LT 授权那条不点名的文案不同。
    # 只锁到那一个参数 ARN,且只有走 resolve:ssm 的 fleet 才授(edge 用官方 AL2023
    # AMI,不走这条路,传 None 就不加)。
    if ssm_image_parameter:
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameters"],
                resources=[
                    scope.format_arn(
                        service="ssm",
                        resource="parameter",
                        # SSM 参数名自带前导 `/`,format_arn 的分隔符会再补一个,
                        # 拼出 `parameter//imagebuilder/...` 不匹配 → 去掉前导斜杠。
                        resource_name=str(ssm_image_parameter).lstrip("/"),
                    )
                ],
            )
        )
    provider = cr.Provider(scope, f"{node_id}Provider", on_event_handler=fn)

    def _shape():
        return _asg_shape_fingerprint(scope, asg.node.default_child, asg_shape)

    tracker = CustomResource(
        scope,
        node_id,
        service_token=provider.service_token,
        properties={
            "AsgName": asg.auto_scaling_group_name,
            "LaunchTemplateId": lt.launch_template_id,
            # 值变化即触发 Update,把 promote 期的临时改动拉回声明状态。
            "LtVersion": lt.latest_version_number,
            # 真值由下面的 Aspect 在 synth 末尾盖上(construct 期算会漏掉之后的改动,
            # 见 _StampAsgShape)。留占位符是为了让"Aspect 没跑"变成看得见的失败。
            "AsgShape": "pending-synth-stamp",
        },
    )
    Aspects.of(Stack.of(scope)).add(
        _StampAsgShape(tracker.node.default_child, _shape)
    )
    tracker.node.add_dependency(asg)
    return tracker


def _sam_build_image_for_host():
    """SAM build image tag for the deploy host's arch (avoids QEMU). pip still
    cross-downloads the aarch64 wheel to match the ARM_64 Lambda."""
    machine = _platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "public.ecr.aws/sam/build-python3.12:latest-arm64"
    return "public.ecr.aws/sam/build-python3.12:latest-x86_64"


def host_golden_ami_parameter_name(gsuffix):
    """SSM parameter holding the current host golden AMI id (#389 v2 block 2).

    Lives here, not in host_image.py, because two stacks must agree on it: the Image
    Builder pipeline WRITES it at distribution and the host LaunchTemplate READS it as
    ``resolve:ssm:``. A name computed independently on each side would drift into an ASG
    whose every launch fails on a nonexistent parameter.

    Under ``/imagebuilder/`` deliberately: the EC2ImageBuilderExecutionPolicy managed
    policy grants ``ssm:PutParameter`` only on that prefix, so any other name needs a
    hand-written policy and fails the bake at distribution time rather than at synth.
    """
    return f"/imagebuilder/openclaw/host-ami{gsuffix or ''}"


def _read_pyproject_version():
    """Best-effort read of the project version so the API can advertise it
    via /system/info. Falls back to "dev" if pyproject.toml is unreadable
    (e.g. during a test that mocks the filesystem)."""
    try:
        import re

        text = (Path(__file__).parent.parent.parent / "pyproject.toml").read_text()
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        return m.group(1) if m else "dev"
    except Exception:
        return "dev"


def _build_vpc(scope, net_cfg):
    """P2b · #187 FR-10 · INTERFACE-CONTRACT §6:三档 VPC。

    - default_vpc: 存量 from_lookup 默认 VPC(host 裸公网,不推荐)。
    - self_managed: 自建 /20,PUBLIC×3(/24)+ PRIVATE_ISOLATED×3(Database /26,
      给 Redis/ElastiCache 独占、无 NAT 出网)+ PRIVATE_WITH_EGRESS×3(/22)+ 3 NAT GW。
    - imported: 客户传 vpc_id + 3 public + 3 private,可选 3 database(Redis 独占);
      缺 database 则 Redis 回落私有子网(向后兼容);其余缺项 raise(fail-loud)。

    切档=改部署代码→重建栈(铁律 #3)。half-config 是隐性错的高发点,
    imported 半配一律 ValueError(不做"部分放行/降级",踩过 too many times)。

    (host/edge/microVM)子网物理隔离,是 AWS 数据面标准分层(DB 子网无 NAT 出网面,
    爆炸半径更小)。CIDR 向后兼容:Database 用 /26 且排在 public 之后、private 之前,
    恰好填进 public(占 10.x.0-2.0/24)与 private(占 10.x.4/8/12.0/22)之间原本闲置
    的 10.x.3.0/24 缝隙 —— 存量 public/private 子网 CIDR 保持 byte-identical,仍是
    /20 不用扩网。
    """
    mode = (net_cfg or {}).get("mode", "default_vpc")
    if mode == "default_vpc":
        return ec2.Vpc.from_lookup(scope, "Vpc", is_default=True)
    if mode == "self_managed":
        sm = net_cfg.get("self_managed") or {}
        cidr = sm.get("cidr") or "10.20.0.0/20"
        return ec2.Vpc(
            scope,
            "Vpc",
            vpc_name="openclaw-vpc",
            ip_addresses=ec2.IpAddresses.cidr(cidr),
            max_azs=3,
            nat_gateways=3,
            # 顺序即 CIDR 分配顺序:Public(/24)→ Database(/26,填 .3.0/24 缝)→
            # Private(/22)。Database 排 private 之前,否则 /26 撞进已切走的 /22 块
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                # Database:Redis/ElastiCache 独占的隔离层,无 NAT(数据库不需出网)。
                ec2.SubnetConfiguration(
                    name="Database",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=26,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=22,
                ),
            ],
        )
    if mode == "imported":
        imp = net_cfg.get("imported") or {}
        vpc_id = (imp.get("vpc_id") or "").strip()
        pubs = list(imp.get("public_subnet_ids") or [])
        privs = list(imp.get("private_subnet_ids") or [])
        dbs = list(imp.get("database_subnet_ids") or [])
        if not (vpc_id and len(pubs) == 3 and len(privs) == 3):
            raise ValueError(
                "network.mode=imported requires non-empty vpc_id + exactly 3 "
                "public_subnet_ids + 3 private_subnet_ids (缺一 fail-loud)"
            )
        # 兼容)。传了就必须是恰好 3 个(跨 3 AZ),半配 fail-loud。
        if dbs and len(dbs) != 3:
            raise ValueError(
                "network.mode=imported: database_subnet_ids 要么留空(Redis 回落私有"
                f"子网),要么恰好 3 个跨 AZ,当前 {len(dbs)} 个(半配 fail-loud)"
            )
        # from_vpc_attributes 要求 AZ 数量与 subnet id 数量对齐(CDK 靠 index 一一
        # 对应),用 stack.availability_zones 前 3 个(scope 是 stack)。跨栈 3 AZ
        # 部署也覆盖 —— 客户传 subnet 时按 AZ 顺序传即可。
        # vpc_cidr_block 必传:现有代码在 SG rule/route 里引用 `vpc.vpc_cidr_block`,
        # 未传会 CannotPerformOperationVpcCidr 崩。客户 imported 时须一起传自家 VPC CIDR。
        _stack_azs = list(scope.availability_zones)[:3]
        _imp_cidr = (imp.get("cidr") or "").strip()
        if not _imp_cidr:
            raise ValueError(
                "network.mode=imported requires imported.cidr (VPC CIDR block, "
                "used by SG rules referencing vpc.vpc_cidr_block)"
            )
        # subnet_type=PRIVATE_ISOLATED 命中);没传则不登记,ha_edge 的 Redis
        # 选子网回落 PRIVATE_WITH_EGRESS(存量行为不变)。
        _attrs = dict(
            vpc_id=vpc_id,
            availability_zones=_stack_azs,
            public_subnet_ids=pubs,
            private_subnet_ids=privs,
            vpc_cidr_block=_imp_cidr,
        )
        if dbs:
            _attrs["isolated_subnet_ids"] = dbs
        return ec2.Vpc.from_vpc_attributes(scope, "Vpc", **_attrs)
    raise ValueError(
        f"network.mode must be 'default_vpc' | 'self_managed' | 'imported', got {mode!r}"
    )
