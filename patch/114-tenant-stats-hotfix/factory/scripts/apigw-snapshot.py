#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Project API Gateway REST API exports and live state into one semantic schema."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "apigw-rest-deployment-v1"
HTTP_METHODS = {
    "delete": "DELETE",
    "get": "GET",
    "head": "HEAD",
    "options": "OPTIONS",
    "patch": "PATCH",
    "post": "POST",
    "put": "PUT",
    "trace": "TRACE",
    "x-amazon-apigateway-any-method": "ANY",
}
METHOD_KEYS = {
    "apiKeyRequired",
    "authorizationScopes",
    "authorizationType",
    "authorizerId",
    "httpMethod",
    "methodIntegration",
    "methodResponses",
    "operationName",
    "requestModels",
    "requestParameters",
    "requestValidatorId",
}
INTEGRATION_KEYS = {
    "cacheKeyParameters",
    "cacheNamespace",
    "connectionId",
    "connectionType",
    "contentHandling",
    "credentials",
    "httpMethod",
    "integrationResponses",
    "integrationTarget",
    "passthroughBehavior",
    "requestParameters",
    "requestTemplates",
    "responseTransferMode",
    "responses",
    "timeoutInMillis",
    "tlsConfig",
    "type",
    "uri",
}
AUTHORIZER_KEYS = {
    "authType",
    "authorizerCredentials",
    "authorizerResultTtlInSeconds",
    "authorizerUri",
    "id",
    "identitySource",
    "identityValidationExpression",
    "name",
    "providerARNs",
    "type",
}
REST_API_KEYS = {
    "apiKeySource",
    "apiStatus",
    "apiStatusMessage",
    "binaryMediaTypes",
    "createdDate",
    "description",
    "disableExecuteApiEndpoint",
    "endpointAccessMode",
    "endpointConfiguration",
    "id",
    "minimumCompressionSize",
    "name",
    "policy",
    "rootResourceId",
    "securityPolicy",
    "tags",
    "version",
    "warnings",
}
EXPORT_TOP_KEYS = {
    "components",
    "externalDocs",
    "info",
    "openapi",
    "paths",
    "security",
    "servers",
    "tags",
    "x-amazon-apigateway-api-key-source",
    "x-amazon-apigateway-binary-media-types",
    "x-amazon-apigateway-endpoint-access-mode",
    "x-amazon-apigateway-endpoint-configuration",
    "x-amazon-apigateway-gateway-responses",
    "x-amazon-apigateway-importexport-version",
    "x-amazon-apigateway-minimum-compression-size",
    "x-amazon-apigateway-policy",
    "x-amazon-apigateway-request-validator",
    "x-amazon-apigateway-request-validators",
    "x-amazon-apigateway-security-policy",
}
OPERATION_KEYS = {
    "deprecated",
    "description",
    "externalDocs",
    "operationId",
    "parameters",
    "requestBody",
    "responses",
    "security",
    "summary",
    "tags",
    "x-amazon-apigateway-integration",
    "x-amazon-apigateway-request-validator",
}
DEFAULT_MODEL_SCHEMAS = {
    "Empty": {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "title": "Empty Schema",
        "type": "object",
    },
    "Error": {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "title": "Error Schema",
        "type": "object",
        "properties": {"message": {"type": "string"}},
    },
}


class UnsupportedError(Exception):
    """The input contains state that cannot be mapped without losing semantics."""


class InputError(Exception):
    """The input or output file is invalid."""


def _unsupported(message: str) -> None:
    raise UnsupportedError(message)


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _unsupported(f"{context} must be an object")
    return value


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        _unsupported(f"{context} must be an array")
    return value


def _known_keys(value: Any, allowed: set[str], context: str) -> dict[str, Any]:
    result = _mapping(value, context)
    unknown = sorted(set(result) - allowed)
    if unknown:
        _unsupported(f"{context} has unknown field(s): {', '.join(unknown)}")
    return result


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str):
        _unsupported(f"{context} must be a string")
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        _unsupported(f"{context} must be a boolean")
    return value


def _integer(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _unsupported(f"{context} must be an integer")
    return value


def _string_list(value: Any, context: str) -> list[str]:
    return sorted(_string(item, f"{context}[]") for item in _array(value, context))


def _string_map(value: Any, context: str) -> dict[str, str]:
    result = _mapping(value, context)
    return {
        _string(key, f"{context} key"): _string(item, f"{context}.{key}")
        for key, item in sorted(result.items())
    }


def _bool_map(value: Any, context: str) -> dict[str, bool]:
    result = _mapping(value, context)
    return {
        _string(key, f"{context} key"): _boolean(item, f"{context}.{key}")
        for key, item in sorted(result.items())
    }


def _load(path: str, label: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"{label}: {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{label}: top level must be an object")
    return value


def _policy(value: Any, context: str) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InputError(f"{context}: invalid JSON policy: {exc}") from exc
    if not isinstance(value, (dict, list)):
        _unsupported(f"{context} must be a JSON object or array")
    return value


def _collection(value: Any, context: str) -> list[Any]:
    data = _known_keys(value, {"items", "position"}, context)
    if data.get("position"):
        _unsupported(f"{context} is paginated; provide a complete response")
    return _array(data.get("items", []), f"{context}.items")


def _path_parameters(path: str) -> list[str]:
    names = [name.removesuffix("+") for name in re.findall(r"\{([^{}]+)\}", path)]
    if len(names) != len(set(names)):
        _unsupported(f"path {path} repeats a path parameter")
    return sorted(names)


def _endpoint(value: Any, context: str) -> dict[str, Any]:
    data = _known_keys(
        value or {},
        {"disableExecuteApiEndpoint", "ipAddressType", "types", "vpcEndpointIds"},
        context,
    )
    vpc_ids = _string_list(data.get("vpcEndpointIds", []), f"{context}.vpcEndpointIds")
    types = _string_list(
        data.get("types", ["PRIVATE"] if vpc_ids else ["EDGE"]),
        f"{context}.types",
    )
    types = [item.upper() for item in types]
    if not types or any(item not in {"EDGE", "PRIVATE", "REGIONAL"} for item in types):
        _unsupported(f"{context}.types contains an unsupported endpoint type")
    default_ip = "dualstack" if types == ["PRIVATE"] else "ipv4"
    ip_type = _string(data.get("ipAddressType", default_ip), f"{context}.ipAddressType")
    if ip_type not in {"dualstack", "ipv4"}:
        _unsupported(f"{context}.ipAddressType is unsupported: {ip_type}")
    return {
        "types": sorted(types),
        "ipAddressType": ip_type,
        "vpcEndpointIds": vpc_ids,
        "disableExecuteApiEndpoint": _boolean(
            data.get("disableExecuteApiEndpoint", False),
            f"{context}.disableExecuteApiEndpoint",
        ),
    }


def _rest_api_from_live(value: Any) -> dict[str, Any]:
    data = _known_keys(value, REST_API_KEYS, "RestApi")
    endpoint = _endpoint(data.get("endpointConfiguration"), "RestApi.endpointConfiguration")
    endpoint["disableExecuteApiEndpoint"] = _boolean(
        data.get(
            "disableExecuteApiEndpoint",
            endpoint["disableExecuteApiEndpoint"],
        ),
        "RestApi.disableExecuteApiEndpoint",
    )
    minimum = data.get("minimumCompressionSize")
    if minimum is not None:
        minimum = _integer(minimum, "RestApi.minimumCompressionSize")
    access_mode = data.get("endpointAccessMode")
    if access_mode is not None:
        access_mode = _string(access_mode, "RestApi.endpointAccessMode").upper()
    return {
        "apiKeySource": _string(
            data.get("apiKeySource", "HEADER"), "RestApi.apiKeySource"
        ).upper(),
        "binaryMediaTypes": _string_list(
            data.get("binaryMediaTypes", []), "RestApi.binaryMediaTypes"
        ),
        "minimumCompressionSize": minimum,
        "disableExecuteApiEndpoint": endpoint.pop("disableExecuteApiEndpoint"),
        "endpointConfiguration": endpoint,
        "endpointAccessMode": access_mode,
        "securityPolicy": _string(
            data.get("securityPolicy", "TLS_1_0"), "RestApi.securityPolicy"
        ),
        "policy": _policy(data.get("policy"), "RestApi.policy"),
    }


def _rest_api_from_export(document: dict[str, Any]) -> dict[str, Any]:
    endpoint_values = []
    if "x-amazon-apigateway-endpoint-configuration" in document:
        endpoint_values.append(document["x-amazon-apigateway-endpoint-configuration"])
    for index, server in enumerate(_array(document.get("servers", []), "servers")):
        server = _known_keys(
            server,
            {
                "description",
                "url",
                "variables",
                "x-amazon-apigateway-endpoint-configuration",
            },
            f"servers[{index}]",
        )
        _string(server.get("url", ""), f"servers[{index}].url")
        variables = _mapping(server.get("variables", {}), f"servers[{index}].variables")
        for name, variable in variables.items():
            if name != "basePath":
                _unsupported(f"servers[{index}] has unknown variable: {name}")
            _known_keys(
                variable,
                {"default", "description", "enum"},
                f"servers[{index}].variables.{name}",
            )
        if "x-amazon-apigateway-endpoint-configuration" in server:
            endpoint_values.append(
                server["x-amazon-apigateway-endpoint-configuration"]
            )
    endpoint = _endpoint(
        endpoint_values[0] if endpoint_values else {},
        "x-amazon-apigateway-endpoint-configuration",
    )
    if any(
        _endpoint(item, "x-amazon-apigateway-endpoint-configuration") != endpoint
        for item in endpoint_values[1:]
    ):
        _unsupported("conflicting endpoint configurations in OpenAPI servers")
    minimum = document.get("x-amazon-apigateway-minimum-compression-size")
    if minimum is not None:
        minimum = _integer(
            minimum, "x-amazon-apigateway-minimum-compression-size"
        )
    access_mode = document.get("x-amazon-apigateway-endpoint-access-mode")
    if access_mode is not None:
        access_mode = _string(
            access_mode, "x-amazon-apigateway-endpoint-access-mode"
        ).upper()
    return {
        "apiKeySource": _string(
            document.get("x-amazon-apigateway-api-key-source", "HEADER"),
            "x-amazon-apigateway-api-key-source",
        ).upper(),
        "binaryMediaTypes": _string_list(
            document.get("x-amazon-apigateway-binary-media-types", []),
            "x-amazon-apigateway-binary-media-types",
        ),
        "minimumCompressionSize": minimum,
        "disableExecuteApiEndpoint": endpoint.pop("disableExecuteApiEndpoint"),
        "endpointConfiguration": endpoint,
        "endpointAccessMode": access_mode,
        "securityPolicy": _string(
            document.get("x-amazon-apigateway-security-policy", "TLS_1_0"),
            "x-amazon-apigateway-security-policy",
        ),
        "policy": _policy(
            document.get("x-amazon-apigateway-policy"),
            "x-amazon-apigateway-policy",
        ),
    }


def _authorizer(detail: Any, context: str, auth_type: Any = None) -> dict[str, Any]:
    data = _known_keys(
        detail,
        {
            "authorizerCredentials",
            "authorizerResultTtlInSeconds",
            "authorizerUri",
            "identitySource",
            "identityValidationExpression",
            "providerARNs",
            "type",
        },
        context,
    )
    ttl = data.get("authorizerResultTtlInSeconds", 300)
    if isinstance(ttl, str) and ttl.isdigit():
        ttl = int(ttl)
    ttl = _integer(ttl, f"{context}.authorizerResultTtlInSeconds")
    return {
        "type": _string(data.get("type"), f"{context}.type").upper(),
        "authType": (
            _string(auth_type, f"{context}.authType") if auth_type is not None else None
        ),
        "authorizerUri": data.get("authorizerUri"),
        "authorizerCredentials": data.get("authorizerCredentials"),
        "identitySource": data.get("identitySource"),
        "identityValidationExpression": data.get("identityValidationExpression"),
        "authorizerResultTtlInSeconds": ttl,
        "providerARNs": _string_list(
            data.get("providerARNs", []), f"{context}.providerARNs"
        ),
    }


def _authorizers_from_live(value: Any) -> tuple[dict[str, str], dict[str, Any]]:
    ids = {}
    result = {}
    for index, item in enumerate(_collection(value, "authorizers")):
        context = f"authorizers.items[{index}]"
        data = _known_keys(item, AUTHORIZER_KEYS, context)
        authorizer_id = _string(data.get("id"), f"{context}.id")
        name = _string(data.get("name"), f"{context}.name")
        if authorizer_id in ids or name in result:
            _unsupported(f"{context} duplicates an authorizer id or name")
        detail = {key: data[key] for key in data if key not in {"id", "name", "authType"}}
        ids[authorizer_id] = name
        result[name] = _authorizer(detail, context, data.get("authType"))
    return ids, result


def _security_schemes(
    components: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any]]:
    schemes = {}
    authorizers = {}
    raw = _mapping(components.get("securitySchemes", {}), "components.securitySchemes")
    allowed = {
        "description",
        "in",
        "name",
        "type",
        "x-amazon-apigateway-authorizer",
        "x-amazon-apigateway-authtype",
    }
    for name, value in sorted(raw.items()):
        context = f"components.securitySchemes.{name}"
        data = _known_keys(value, allowed, context)
        scheme_type = _string(data.get("type"), f"{context}.type")
        location = _string(data.get("in"), f"{context}.in")
        source_name = _string(data.get("name"), f"{context}.name")
        if scheme_type != "apiKey" or location not in {"header", "query"}:
            _unsupported(f"{context} is not a REST API apiKey security scheme")
        if "x-amazon-apigateway-authorizer" in data:
            schemes[name] = "authorizer"
            authorizers[name] = _authorizer(
                data["x-amazon-apigateway-authorizer"],
                f"{context}.x-amazon-apigateway-authorizer",
                data.get("x-amazon-apigateway-authtype"),
            )
        elif str(data.get("x-amazon-apigateway-authtype", "")).lower() == "awssigv4":
            schemes[name] = "iam"
        elif location == "header" and source_name.lower() == "x-api-key":
            schemes[name] = "api_key"
        else:
            _unsupported(f"{context} cannot be mapped to API key, IAM, or authorizer")
    return schemes, authorizers


def _validators_from_live(value: Any) -> tuple[dict[str, str], dict[str, Any]]:
    ids = {}
    result = {}
    allowed = {
        "id",
        "name",
        "validateRequestBody",
        "validateRequestParameters",
    }
    for index, item in enumerate(_collection(value, "request validators")):
        context = f"request validators.items[{index}]"
        data = _known_keys(item, allowed, context)
        validator_id = _string(data.get("id"), f"{context}.id")
        name = _string(data.get("name"), f"{context}.name")
        if validator_id in ids or name in result:
            _unsupported(f"{context} duplicates a validator id or name")
        ids[validator_id] = name
        result[name] = {
            "validateRequestBody": _boolean(
                data.get("validateRequestBody", False),
                f"{context}.validateRequestBody",
            ),
            "validateRequestParameters": _boolean(
                data.get("validateRequestParameters", False),
                f"{context}.validateRequestParameters",
            ),
        }
    return ids, result


def _validators_from_export(value: Any) -> dict[str, Any]:
    result = {}
    for name, item in sorted(_mapping(value or {}, "request validators").items()):
        context = f"request validators.{name}"
        data = _known_keys(
            item,
            {"validateRequestBody", "validateRequestParameters"},
            context,
        )
        result[name] = {
            "validateRequestBody": _boolean(
                data.get("validateRequestBody", False),
                f"{context}.validateRequestBody",
            ),
            "validateRequestParameters": _boolean(
                data.get("validateRequestParameters", False),
                f"{context}.validateRequestParameters",
            ),
        }
    return result


def _models_from_live(value: Any) -> dict[str, Any]:
    result = {}
    for index, item in enumerate(_collection(value, "models")):
        context = f"models.items[{index}]"
        data = _known_keys(
            item, {"contentType", "description", "id", "name", "schema"}, context
        )
        name = _string(data.get("name"), f"{context}.name")
        content_type = _string(data.get("contentType"), f"{context}.contentType")
        if content_type != "application/json":
            _unsupported(f"{context} is not an application/json model")
        try:
            schema = json.loads(_string(data.get("schema"), f"{context}.schema"))
        except json.JSONDecodeError as exc:
            raise InputError(f"{context}.schema: {exc}") from exc
        if name in DEFAULT_MODEL_SCHEMAS and schema == DEFAULT_MODEL_SCHEMAS[name]:
            continue
        if name in result:
            _unsupported(f"{context} duplicates model {name}")
        result[name] = {"contentType": content_type, "schema": schema}
    return result


def _models_from_export(components: dict[str, Any]) -> dict[str, Any]:
    result = {}
    schemas = _mapping(components.get("schemas", {}), "components.schemas")
    for name, schema in sorted(schemas.items()):
        _mapping(schema, f"components.schemas.{name}")
        if name in DEFAULT_MODEL_SCHEMAS and schema == DEFAULT_MODEL_SCHEMAS[name]:
            continue
        result[name] = {"contentType": "application/json", "schema": schema}
    return result


def _gateway_response(value: Any, context: str) -> dict[str, Any]:
    data = _known_keys(
        value,
        {"responseParameters", "responseTemplates", "statusCode"},
        context,
    )
    status = data.get("statusCode")
    if status is not None:
        status = _string(status, f"{context}.statusCode")
    return {
        "statusCode": status,
        "responseParameters": _string_map(
            data.get("responseParameters", {}), f"{context}.responseParameters"
        ),
        "responseTemplates": _string_map(
            data.get("responseTemplates", {}), f"{context}.responseTemplates"
        ),
    }


def _gateway_responses_from_live(value: Any) -> dict[str, Any]:
    result = {}
    for index, item in enumerate(_collection(value, "gateway responses")):
        context = f"gateway responses.items[{index}]"
        data = _known_keys(
            item,
            {
                "defaultResponse",
                "responseParameters",
                "responseTemplates",
                "responseType",
                "statusCode",
            },
            context,
        )
        if _boolean(data.get("defaultResponse", False), f"{context}.defaultResponse"):
            continue
        response_type = _string(data.get("responseType"), f"{context}.responseType")
        if response_type in result:
            _unsupported(f"{context} duplicates {response_type}")
        result[response_type] = _gateway_response(
            {key: value for key, value in data.items() if key not in {"defaultResponse", "responseType"}},
            context,
        )
    return result


def _gateway_responses_from_export(value: Any) -> dict[str, Any]:
    return {
        name: _gateway_response(item, f"gateway responses.{name}")
        for name, item in sorted(_mapping(value or {}, "gateway responses").items())
    }


def _integration_response(value: Any, context: str) -> dict[str, Any]:
    data = _known_keys(
        value,
        {
            "contentHandling",
            "responseParameters",
            "responseTemplates",
            "selectionPattern",
            "statusCode",
        },
        context,
    )
    status = _string(data.get("statusCode"), f"{context}.statusCode")
    handling = data.get("contentHandling")
    if handling is not None:
        handling = _string(handling, f"{context}.contentHandling").upper()
    return {
        "statusCode": status,
        "responseParameters": _string_map(
            data.get("responseParameters", {}), f"{context}.responseParameters"
        ),
        "responseTemplates": _string_map(
            data.get("responseTemplates", {}), f"{context}.responseTemplates"
        ),
        "contentHandling": handling,
    }


def _integration(value: Any, context: str, resource_id: str | None) -> Any:
    if value is None:
        return None
    data = _known_keys(value, INTEGRATION_KEYS, context)
    integration_type = _string(data.get("type"), f"{context}.type").upper()
    cache_namespace = data.get("cacheNamespace")
    if cache_namespace is None or cache_namespace == resource_id:
        cache_namespace = "$resource"
    else:
        cache_namespace = _string(cache_namespace, f"{context}.cacheNamespace")
    tls = _known_keys(
        data.get("tlsConfig", {}),
        {"insecureSkipVerification"},
        f"{context}.tlsConfig",
    )
    responses = {}
    if "responses" in data:
        for selector, response in sorted(
            _mapping(data["responses"], f"{context}.responses").items()
        ):
            responses[selector] = _integration_response(
                response, f"{context}.responses.{selector}"
            )
    if "integrationResponses" in data:
        for status, response in sorted(
            _mapping(
                data["integrationResponses"], f"{context}.integrationResponses"
            ).items()
        ):
            response_data = _mapping(response, f"{context}.integrationResponses.{status}")
            selector = response_data.get("selectionPattern") or "default"
            if selector in responses:
                _unsupported(f"{context} has duplicate integration selector {selector}")
            normalized = dict(response_data)
            normalized.setdefault("statusCode", str(status))
            responses[selector] = _integration_response(
                normalized, f"{context}.integrationResponses.{status}"
            )
    handling = data.get("contentHandling")
    if handling is not None:
        handling = _string(handling, f"{context}.contentHandling").upper()
    http_method = data.get("httpMethod")
    if http_method is not None:
        http_method = _string(http_method, f"{context}.httpMethod").upper()
    connection_type = _string(
        data.get("connectionType", "INTERNET"), f"{context}.connectionType"
    ).upper()
    passthrough = _string(
        data.get("passthroughBehavior", "WHEN_NO_MATCH"),
        f"{context}.passthroughBehavior",
    ).upper()
    return {
        "type": integration_type,
        "httpMethod": http_method,
        "uri": data.get("uri"),
        "credentials": data.get("credentials"),
        "cacheNamespace": cache_namespace,
        "cacheKeyParameters": _string_list(
            data.get("cacheKeyParameters", []), f"{context}.cacheKeyParameters"
        ),
        "connectionType": connection_type,
        "connectionId": data.get("connectionId"),
        "integrationTarget": data.get("integrationTarget"),
        "requestParameters": _string_map(
            data.get("requestParameters", {}), f"{context}.requestParameters"
        ),
        "requestTemplates": _string_map(
            data.get("requestTemplates", {}), f"{context}.requestTemplates"
        ),
        "passthroughBehavior": passthrough,
        "contentHandling": handling,
        "timeoutInMillis": _integer(
            data.get("timeoutInMillis", 29000), f"{context}.timeoutInMillis"
        ),
        "responseTransferMode": _string(
            data.get("responseTransferMode", "BUFFERED"),
            f"{context}.responseTransferMode",
        ).upper(),
        "tlsConfig": {
            "insecureSkipVerification": _boolean(
                tls.get("insecureSkipVerification", False),
                f"{context}.tlsConfig.insecureSkipVerification",
            )
        },
        "integrationResponses": responses,
    }


def _model_reference(value: Any, context: str) -> str:
    schema = _known_keys(value, {"$ref"}, context)
    reference = _string(schema.get("$ref"), f"{context}.$ref")
    prefix = "#/components/schemas/"
    if not reference.startswith(prefix) or not reference[len(prefix) :]:
        _unsupported(f"{context} must be a local model reference")
    return reference[len(prefix) :]


def _content_models(value: Any, context: str) -> dict[str, str]:
    result = {}
    for content_type, media in sorted(_mapping(value or {}, context).items()):
        media = _known_keys(
            media, {"encoding", "example", "examples", "schema"}, f"{context}.{content_type}"
        )
        if media.get("encoding"):
            _unsupported(f"{context}.{content_type}.encoding cannot be mapped")
        result[content_type] = _model_reference(
            media.get("schema"), f"{context}.{content_type}.schema"
        )
    return result


def _method_responses_from_export(value: Any, context: str) -> dict[str, Any]:
    result = {}
    for status, response in sorted(_mapping(value or {}, context).items()):
        if not re.fullmatch(r"[1-5]\d\d", status):
            _unsupported(f"{context} has unsupported status key: {status}")
        data = _known_keys(
            response, {"content", "description", "headers"}, f"{context}.{status}"
        )
        parameters = {}
        for name, header in sorted(
            _mapping(data.get("headers", {}), f"{context}.{status}.headers").items()
        ):
            header_data = _known_keys(
                header,
                {
                    "deprecated",
                    "description",
                    "example",
                    "examples",
                    "explode",
                    "required",
                    "schema",
                    "style",
                },
                f"{context}.{status}.headers.{name}",
            )
            schema = _known_keys(
                header_data.get("schema", {}),
                {"type"},
                f"{context}.{status}.headers.{name}.schema",
            )
            if schema.get("type") not in (None, "string"):
                _unsupported(f"{context}.{status}.headers.{name} is not a string")
            required = header_data.get("required", True)
            if required is not True:
                _unsupported(f"{context}.{status}.headers.{name} is optional")
            parameters[f"method.response.header.{name}"] = True
        result[status] = {
            "responseParameters": parameters,
            "responseModels": _content_models(
                data.get("content", {}), f"{context}.{status}.content"
            ),
        }
    return result


def _method_responses_from_live(value: Any, context: str) -> dict[str, Any]:
    result = {}
    for status, response in sorted(_mapping(value or {}, context).items()):
        response_context = f"{context}.{status}"
        data = _known_keys(
            response,
            {"responseModels", "responseParameters", "statusCode"},
            response_context,
        )
        if str(data.get("statusCode", status)) != str(status):
            _unsupported(f"{response_context}.statusCode does not match its key")
        parameters = _bool_map(
            data.get("responseParameters", {}),
            f"{response_context}.responseParameters",
        )
        if any(required is not True for required in parameters.values()):
            _unsupported(f"{response_context} has optional response parameters")
        result[str(status)] = {
            "responseParameters": parameters,
            "responseModels": _string_map(
                data.get("responseModels", {}), f"{response_context}.responseModels"
            ),
        }
    return result


def _oas_parameters(
    value: Any, context: str, path_parameter_names: set[str]
) -> dict[str, bool]:
    result = {}
    allowed = {
        "deprecated",
        "description",
        "example",
        "examples",
        "explode",
        "in",
        "name",
        "required",
        "schema",
        "style",
    }
    for index, parameter in enumerate(_array(value or [], context)):
        item_context = f"{context}[{index}]"
        data = _known_keys(parameter, allowed, item_context)
        name = _string(data.get("name"), f"{item_context}.name")
        location = _string(data.get("in"), f"{item_context}.in")
        required = _boolean(data.get("required", False), f"{item_context}.required")
        schema = _known_keys(
            data.get("schema", {}), {"type"}, f"{item_context}.schema"
        )
        if schema.get("type") not in (None, "string"):
            _unsupported(f"{item_context}.schema is not a string")
        if location == "path":
            if name not in path_parameter_names or required is not True:
                _unsupported(f"{item_context} does not match a required URL placeholder")
            continue
        location_name = "querystring" if location == "query" else location
        if location_name not in {"header", "querystring"}:
            _unsupported(f"{item_context}.in is unsupported: {location}")
        key = f"method.request.{location_name}.{name}"
        if key in result and result[key] != required:
            _unsupported(f"{item_context} conflicts with another parameter declaration")
        result[key] = required
    return result


def _live_request_parameters(
    value: Any, context: str, path_parameter_names: set[str]
) -> dict[str, bool]:
    parameters = _bool_map(value or {}, context)
    for key in list(parameters):
        prefix = "method.request.path."
        if key.startswith(prefix):
            name = key[len(prefix) :]
            if name not in path_parameter_names or parameters[key] is not True:
                _unsupported(f"{context}.{key} does not match a required URL placeholder")
            parameters.pop(key)
    return parameters


def _method_auth_from_export(
    security: Any,
    summary: dict[str, Any],
    schemes: dict[str, str],
    context: str,
) -> tuple[str | None, list[str]]:
    authorizers = {}
    api_key = False
    iam = False
    for index, requirement in enumerate(_array(security or [], f"{context}.security")):
        requirement = _mapping(requirement, f"{context}.security[{index}]")
        for name, scopes in requirement.items():
            if name not in schemes:
                _unsupported(f"{context}.security references unknown scheme {name}")
            kind = schemes[name]
            parsed_scopes = _string_list(scopes, f"{context}.security.{name}")
            if kind == "api_key":
                api_key = True
            elif kind == "iam":
                iam = True
            else:
                authorizers[name] = parsed_scopes
    expected_key = _boolean(
        summary.get("apiKeyRequired", False), f"{context}.apiKeyRequired"
    )
    if api_key != expected_key:
        _unsupported(f"{context} API-key security disagrees with apiSummary")
    authorization_type = _string(
        summary.get("authorizationType", "NONE"), f"{context}.authorizationType"
    ).upper()
    if authorization_type == "NONE" and (iam or authorizers):
        _unsupported(f"{context} has security for a NONE method")
    if authorization_type == "AWS_IAM" and (not iam or authorizers):
        _unsupported(f"{context} does not have exactly AWS_IAM security")
    if authorization_type in {"CUSTOM", "COGNITO_USER_POOLS"} and len(authorizers) != 1:
        _unsupported(f"{context} does not reference exactly one authorizer")
    if authorization_type not in {"NONE", "AWS_IAM", "CUSTOM", "COGNITO_USER_POOLS"}:
        _unsupported(f"{context} has unsupported authorizationType {authorization_type}")
    if authorizers:
        name = next(iter(authorizers))
        return name, authorizers[name]
    return None, []


def _summary(value: Any) -> dict[str, dict[str, dict[str, Any]]]:
    data = _known_keys(
        value, {"apiSummary", "createdDate", "description", "id"}, "deployment"
    )
    result = {}
    for path, methods in sorted(
        _mapping(data.get("apiSummary", {}), "deployment.apiSummary").items()
    ):
        result[path] = {}
        for method, snapshot in sorted(
            _mapping(methods, f"deployment.apiSummary.{path}").items()
        ):
            result[path][method.upper()] = _known_keys(
                snapshot,
                {"apiKeyRequired", "authorizationType"},
                f"deployment.apiSummary.{path}.{method}",
            )
    return result


def _operation_from_export(
    operation: Any,
    summary: dict[str, Any],
    schemes: dict[str, str],
    validators: dict[str, Any],
    inherited_security: Any,
    inherited_validator: Any,
    path_parameters: list[Any],
    placeholders: set[str],
    context: str,
) -> dict[str, Any]:
    data = _known_keys(operation, OPERATION_KEYS, context)
    parameters = _oas_parameters(path_parameters, f"{context}.pathParameters", placeholders)
    own_parameters = _oas_parameters(
        data.get("parameters", []), f"{context}.parameters", placeholders
    )
    for key, required in own_parameters.items():
        if key in parameters and parameters[key] != required:
            _unsupported(f"{context}.parameters conflicts with path-level parameter {key}")
        parameters[key] = required
    validator = data.get(
        "x-amazon-apigateway-request-validator", inherited_validator
    )
    if validator is not None:
        validator = _string(validator, f"{context}.requestValidator")
        if validator not in validators:
            _unsupported(f"{context} references unknown request validator {validator}")
    request_body = data.get("requestBody")
    request_models = {}
    if request_body is not None:
        body = _known_keys(
            request_body, {"content", "description", "required"}, f"{context}.requestBody"
        )
        request_models = _content_models(
            body.get("content", {}), f"{context}.requestBody.content"
        )
    security = data.get("security", inherited_security)
    authorizer, scopes = _method_auth_from_export(
        security, summary, schemes, context
    )
    operation_name = data.get("operationId")
    if operation_name is not None:
        operation_name = _string(operation_name, f"{context}.operationId")
    return {
        "authorizationType": _string(
            summary.get("authorizationType", "NONE"), f"{context}.authorizationType"
        ).upper(),
        "apiKeyRequired": _boolean(
            summary.get("apiKeyRequired", False), f"{context}.apiKeyRequired"
        ),
        "authorizer": authorizer,
        "authorizationScopes": scopes,
        "operationName": operation_name,
        "requestParameters": parameters,
        "requestModels": request_models,
        "requestValidator": validator,
        "methodResponses": _method_responses_from_export(
            data.get("responses", {}), f"{context}.responses"
        ),
        "integration": _integration(
            data.get("x-amazon-apigateway-integration"),
            f"{context}.integration",
            None,
        ),
    }


def _paths_from_export(
    document: dict[str, Any],
    summary: dict[str, dict[str, dict[str, Any]]],
    schemes: dict[str, str],
    validators: dict[str, Any],
) -> dict[str, Any]:
    result = {}
    root_security = document.get("security", [])
    root_validator = document.get("x-amazon-apigateway-request-validator")
    for path, path_item in sorted(_mapping(document.get("paths", {}), "paths").items()):
        context = f"paths.{path}"
        allowed = set(HTTP_METHODS) | {"parameters"}
        data = _known_keys(path_item, allowed, context)
        placeholders = set(_path_parameters(path))
        path_parameters = data.get("parameters", [])
        methods = {}
        for source_name, method_name in HTTP_METHODS.items():
            if source_name not in data:
                continue
            snapshot = summary.get(path, {}).get(method_name)
            if snapshot is None:
                _unsupported(f"{context}.{source_name} is missing from apiSummary")
            methods[method_name] = _operation_from_export(
                data[source_name],
                snapshot,
                schemes,
                validators,
                root_security,
                root_validator,
                path_parameters,
                placeholders,
                f"{context}.{source_name}",
            )
        result[path] = {
            "pathParameters": sorted(placeholders),
            "methods": methods,
        }
    projected_methods = {
        (path, method)
        for path, item in result.items()
        for method in item["methods"]
    }
    summary_methods = {
        (path, method) for path, methods in summary.items() for method in methods
    }
    if projected_methods != summary_methods:
        missing = sorted(summary_methods - projected_methods)
        extra = sorted(projected_methods - summary_methods)
        _unsupported(f"OpenAPI/apiSummary method sets differ: missing={missing}, extra={extra}")
    return result


def _operation_from_live(
    method: Any,
    method_name: str,
    resource_id: str,
    placeholders: set[str],
    authorizer_ids: dict[str, str],
    validator_ids: dict[str, str],
    context: str,
) -> dict[str, Any]:
    data = _known_keys(method, METHOD_KEYS, context)
    declared_method = _string(data.get("httpMethod", method_name), f"{context}.httpMethod")
    if declared_method.upper() != method_name:
        _unsupported(f"{context}.httpMethod does not match its map key")
    authorization_type = _string(
        data.get("authorizationType", "NONE"), f"{context}.authorizationType"
    ).upper()
    authorizer = None
    authorizer_id = data.get("authorizerId")
    if authorizer_id is not None:
        authorizer_id = _string(authorizer_id, f"{context}.authorizerId")
        authorizer = authorizer_ids.get(authorizer_id)
        if authorizer is None:
            _unsupported(f"{context} references unread authorizer {authorizer_id}")
    if authorization_type in {"CUSTOM", "COGNITO_USER_POOLS"} and authorizer is None:
        _unsupported(f"{context} requires an authorizer")
    if authorization_type in {"NONE", "AWS_IAM"} and authorizer is not None:
        _unsupported(f"{context} has an authorizer for {authorization_type}")
    validator = None
    validator_id = data.get("requestValidatorId")
    if validator_id is not None:
        validator_id = _string(validator_id, f"{context}.requestValidatorId")
        validator = validator_ids.get(validator_id)
        if validator is None:
            _unsupported(f"{context} references unread validator {validator_id}")
    operation_name = data.get("operationName")
    if operation_name is not None:
        operation_name = _string(operation_name, f"{context}.operationName")
    return {
        "authorizationType": authorization_type,
        "apiKeyRequired": _boolean(
            data.get("apiKeyRequired", False), f"{context}.apiKeyRequired"
        ),
        "authorizer": authorizer,
        "authorizationScopes": _string_list(
            data.get("authorizationScopes", []), f"{context}.authorizationScopes"
        ),
        "operationName": operation_name,
        "requestParameters": _live_request_parameters(
            data.get("requestParameters", {}),
            f"{context}.requestParameters",
            placeholders,
        ),
        "requestModels": _string_map(
            data.get("requestModels", {}), f"{context}.requestModels"
        ),
        "requestValidator": validator,
        "methodResponses": _method_responses_from_live(
            data.get("methodResponses", {}), f"{context}.methodResponses"
        ),
        "integration": _integration(
            data.get("methodIntegration"),
            f"{context}.methodIntegration",
            resource_id,
        ),
    }


def _paths_from_live(
    value: Any,
    authorizer_ids: dict[str, str],
    validator_ids: dict[str, str],
) -> dict[str, Any]:
    result = {}
    resource_keys = {"id", "parentId", "path", "pathPart", "resourceMethods"}
    for index, resource in enumerate(_collection(value, "resources")):
        context = f"resources.items[{index}]"
        data = _known_keys(resource, resource_keys, context)
        resource_id = _string(data.get("id"), f"{context}.id")
        path = _string(data.get("path"), f"{context}.path")
        if path in result:
            _unsupported(f"{context} duplicates path {path}")
        placeholders = set(_path_parameters(path))
        methods = {}
        for method_name, method in sorted(
            _mapping(data.get("resourceMethods", {}), f"{context}.resourceMethods").items()
        ):
            normalized = method_name.upper()
            if normalized not in set(HTTP_METHODS.values()):
                _unsupported(f"{context} has unsupported HTTP method {method_name}")
            methods[normalized] = _operation_from_live(
                method,
                normalized,
                resource_id,
                placeholders,
                authorizer_ids,
                validator_ids,
                f"{context}.resourceMethods.{method_name}",
            )
        result[path] = {
            "pathParameters": sorted(placeholders),
            "methods": methods,
        }
    return result


def project_from_export(document: dict[str, Any], deployment: dict[str, Any]) -> dict[str, Any]:
    _known_keys(document, EXPORT_TOP_KEYS, "OpenAPI")
    version = _string(document.get("openapi"), "OpenAPI.openapi")
    if not version.startswith("3."):
        _unsupported(f"OpenAPI version is not 3.x: {version}")
    info = _known_keys(
        document.get("info", {}),
        {
            "contact",
            "description",
            "license",
            "summary",
            "termsOfService",
            "title",
            "version",
        },
        "OpenAPI.info",
    )
    if "title" in info:
        _string(info["title"], "OpenAPI.info.title")
    components = _known_keys(
        document.get("components", {}),
        {"schemas", "securitySchemes"},
        "OpenAPI.components",
    )
    schemes, authorizers = _security_schemes(components)
    validators = _validators_from_export(
        document.get("x-amazon-apigateway-request-validators")
    )
    summary = _summary(deployment)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "restApi": _rest_api_from_export(document),
        "authorizers": authorizers,
        "models": _models_from_export(components),
        "requestValidators": validators,
        "gatewayResponses": _gateway_responses_from_export(
            document.get("x-amazon-apigateway-gateway-responses")
        ),
        "paths": _paths_from_export(document, summary, schemes, validators),
    }


def project_from_live(
    resources: dict[str, Any],
    authorizers_raw: dict[str, Any],
    rest_api: dict[str, Any],
    models_raw: dict[str, Any],
    validators_raw: dict[str, Any],
    gateway_responses_raw: dict[str, Any],
) -> dict[str, Any]:
    authorizer_ids, authorizers = _authorizers_from_live(authorizers_raw)
    validator_ids, validators = _validators_from_live(validators_raw)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "restApi": _rest_api_from_live(rest_api),
        "authorizers": authorizers,
        "models": _models_from_live(models_raw),
        "requestValidators": validators,
        "gatewayResponses": _gateway_responses_from_live(gateway_responses_raw),
        "paths": _paths_from_live(resources, authorizer_ids, validator_ids),
    }


def _write(path: str, value: dict[str, Any], input_paths: list[str]) -> None:
    output = Path(path)
    resolved_output = output.resolve()
    if any(resolved_output == Path(item).resolve() for item in input_paths):
        raise InputError("output path must not overwrite an input")
    if not output.parent.exists():
        raise InputError(f"output directory does not exist: {output.parent}")
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if temporary and os.path.exists(temporary):
            trash = Path.home() / "Documents" / "trashllm" / "oc-patch-temp"
            try:
                trash.mkdir(parents=True, exist_ok=True)
                os.replace(
                    temporary,
                    trash / f"{Path(temporary).name}.{os.getpid()}",
                )
            except OSError:
                # Preserve the failed temporary file in place when trash is unavailable.
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project API Gateway REST API state into stable deployment semantics."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    exported = subparsers.add_parser("from-export")
    exported.add_argument("oas30_export")
    exported.add_argument("deployment_api_summary")
    exported.add_argument("output")
    live = subparsers.add_parser("from-live")
    live.add_argument("resources")
    live.add_argument("authorizers")
    live.add_argument("rest_api")
    live.add_argument("models")
    live.add_argument("request_validators")
    live.add_argument("gateway_responses")
    live.add_argument("output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "from-export":
            inputs = [args.oas30_export, args.deployment_api_summary]
            projection = project_from_export(
                _load(args.oas30_export, "OpenAPI export"),
                _load(args.deployment_api_summary, "deployment apiSummary"),
            )
            output = args.output
        else:
            inputs = [
                args.resources,
                args.authorizers,
                args.rest_api,
                args.models,
                args.request_validators,
                args.gateway_responses,
            ]
            projection = project_from_live(
                _load(args.resources, "resources"),
                _load(args.authorizers, "authorizers"),
                _load(args.rest_api, "RestApi"),
                _load(args.models, "models"),
                _load(args.request_validators, "request validators"),
                _load(args.gateway_responses, "gateway responses"),
            )
            output = args.output
        _write(output, projection, inputs)
        return 0
    except UnsupportedError as exc:
        print(f"UNSUPPORTED: {exc}", file=sys.stderr)
        return 2
    except InputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
