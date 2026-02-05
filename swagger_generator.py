import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple


class SwaggerToFrontendGenerator:
    def __init__(self, swagger_data: Dict[str, Any], request_import_path: str = '@/utils/request'):
        self.swagger_data = swagger_data
        self.components = swagger_data.get('components', {}).get('schemas', {})
        self.paths = swagger_data.get('paths', {})
        servers = swagger_data.get('servers', [])
        self.base_url = servers[0].get('url', '') if servers else ''
        self.generated_interfaces: Set[str] = set()
        self.current_ref_chain: Set[str] = set()
        self.request_import_path = request_import_path
        self._module_func_names: Dict[str, Set[str]] = {}
        self._global_func_names: Set[str] = set()
        self.existing_mappings: Dict[Tuple[str, str], str] = {}  # (method, url) -> function_name
        self.operation_types: Dict[str, Dict[str, str]] = {}
        self.mapping_file_name = 'api-mapping.json'

    def resolve_ref(self, ref: str) -> Optional[Dict[str, Any]]:
        """解析 $ref 引用"""
        if ref.startswith('#/components/schemas/'):
            schema_name = ref.split('/')[-1]
            return self.components.get(schema_name)
        if ref.startswith('#/definitions/'):  # 兼容 OpenAPI 2.0
            schema_name = ref.split('/')[-1]
            return self.components.get(schema_name)
        return None

    def generate_typescript_type(self, schema: Dict[str, Any], context: str = '', depth: int = 0) -> str:
        """生成TypeScript类型定义，带递归深度控制"""
        if depth > 10:  # 防止无限递归
            return 'any'

        if not schema:
            return 'any'

        if '$ref' in schema:
            ref = schema['$ref']
            ref_name = ref.split('/')[-1]

            # 对于组件引用，直接返回类型名称，让生成的代码使用已定义的接口
            if ref.startswith('#/components/schemas/') or ref.startswith('#/definitions/'):
                return ref_name

            # 对于其他引用，尝试解析
            ref_schema = self.resolve_ref(ref)
            if ref_schema:
                return self.generate_typescript_type(ref_schema, context, depth + 1)
            return ref_name  # 返回引用类型名

        schema_type = schema.get('type', 'object')

        if schema_type == 'string':
            # 处理枚举
            if 'enum' in schema:
                enum_values = ' | '.join([f'"{v}"' for v in schema['enum']])
                return enum_values
            return 'string'
        if schema_type == 'number' or schema_type == 'integer':
            return 'number'
        if schema_type == 'boolean':
            return 'boolean'
        if schema_type == 'array':
            items_schema = schema.get('items', {})
            item_type = self.generate_typescript_type(items_schema, context, depth + 1)
            return f'{item_type}[]'
        if schema_type == 'object':
            properties = schema.get('properties', {})
            if properties:
                # 对于内联对象，不生成接口，直接生成类型
                return self.generate_inline_interface(properties, context, depth + 1)
            additional_props = schema.get('additionalProperties')
            if additional_props:
                value_type = self.generate_typescript_type(additional_props, context, depth + 1)
                return f'Record<string, {value_type}>'
            return '{ [key: string]: any }'

        return 'any'

    def generate_inline_interface(self, properties: Dict[str, Any], context: str = '', depth: int = 0) -> str:
        """生成内联接口类型（不创建命名接口）"""
        if depth > 5:
            return 'any'

        lines = []
        for prop_name, prop_schema in properties.items():
            prop_type = self.generate_typescript_type(prop_schema, f"{context}_{prop_name}", depth + 1)
            # 对于内联对象，required 字段通常在父级 schema 中定义
            # 这里假设所有属性都是可选的，因为内联对象通常用于响应或请求体的一部分
            optional_marker = '?'
            lines.append(f'{prop_name}{optional_marker}: {prop_type};')

        return '{ ' + ' '.join(lines) + ' }'

    def generate_interface(self, schema_name: str, schema: Dict[str, Any]) -> str:
        """生成命名接口"""
        if schema_name in self.generated_interfaces:
            return f'// interface {schema_name} 已生成\n'

        self.generated_interfaces.add(schema_name)

        properties = schema.get('properties', {})
        required_fields = schema.get('required', [])

        if not properties:
            return f'interface {schema_name} {self.generate_typescript_type(schema, schema_name)}'

        lines = []
        for prop_name, prop_schema in properties.items():
            prop_type = self.generate_typescript_type(prop_schema, f"{schema_name}_{prop_name}")
            required = prop_name in required_fields
            optional_marker = '' if required else '?'

            # 添加注释
            description = prop_schema.get('description', '')
            if description:
                lines.append(f'  /** {description} */')
            lines.append(f'  {prop_name}{optional_marker}: {prop_type};')

        return f'export interface {schema_name} {{\n' + '\n'.join(lines) + '\n}'

    def generate_all_interfaces(self) -> str:
        """生成所有组件的TypeScript接口"""
        interfaces = []

        # 先处理没有复杂嵌套的简单接口
        simple_schemas = []
        complex_schemas = []

        for schema_name, schema in self.components.items():
            try:
                if self.is_simple_schema(schema):
                    simple_schemas.append((schema_name, schema))
                else:
                    complex_schemas.append((schema_name, schema))
            except Exception as exc:
                print(f"警告: 分析接口 {schema_name} 时出错: {exc}")
                # 生成一个简单的备用接口
                interfaces.append(f"interface {schema_name} {{ /* 分析错误 */ }}")

        # 先生成简单接口
        for schema_name, schema in simple_schemas:
            try:
                interface_code = self.generate_interface(schema_name, schema)
                interfaces.append(interface_code)
            except Exception as exc:
                print(f"警告: 生成接口 {schema_name} 时出错: {exc}")
                interfaces.append(f"interface {schema_name} {{ /* 生成错误 */ }}")

        # 再生成复杂接口
        for schema_name, schema in complex_schemas:
            try:
                interface_code = self.generate_interface(schema_name, schema)
                interfaces.append(interface_code)
            except Exception as exc:
                print(f"警告: 生成接口 {schema_name} 时出错: {exc}")
                interfaces.append(f"interface {schema_name} {{ /* 生成错误 */ }}")

        return '\n\n'.join(interfaces)

    def is_simple_schema(self, schema: Dict[str, Any]) -> bool:
        """判断是否为简单schema（没有复杂嵌套）"""
        if '$ref' in schema:
            return False

        schema_type = schema.get('type')
        if schema_type == 'array':
            items = schema.get('items', {})
            return not ('$ref' in items or items.get('type') == 'object')
        if schema_type == 'object':
            properties = schema.get('properties', {})
            for prop_schema in properties.values():
                if '$ref' in prop_schema or prop_schema.get('type') in ['object', 'array']:
                    return False
            return True
        return True

    def _register_operation_types(self, function_name: str, request_type: Optional[str], response_type: str) -> None:
        if function_name not in self.operation_types:
            self.operation_types[function_name] = {}
        if request_type:
            self.operation_types[function_name]['request'] = request_type
        self.operation_types[function_name]['response'] = response_type

    def _operation_type_name(self, function_name: str, suffix: str) -> str:
        base = function_name[:1].upper() + function_name[1:]
        return f"{base}{suffix}"

    def generate_api_function(self, path: str, method: str, operation: Dict[str, Any]) -> Tuple[str, str]:
        """生成单个API请求函数，兼容现有的axios封装"""
        operation_id = operation.get('operationId', '')
        tags = operation.get('tags', ['Default'])
        module_name = tags[0] if tags else 'Default'
        summary = operation.get('summary', '')

        function_name = self._build_function_name(module_name, path, method, operation_id)

        # 生成参数类型
        request_body_type = 'any'
        has_query_params = False
        has_path_params = False
        request_type_alias = None

        # 处理请求体
        request_body = operation.get('requestBody')
        if request_body:
            content = request_body.get('content', {})
            for _content_type, media_type in content.items():
                if 'schema' in media_type:
                    request_schema = media_type['schema']
                    request_body_type = self.generate_typescript_type(
                        request_schema,
                        f"{function_name}Request",
                    )
                    break
            request_type_alias = self._operation_type_name(function_name, 'Request')

        response_type = self.get_response_type(operation)
        response_type_alias = self._operation_type_name(function_name, 'Response')
        self._register_operation_types(function_name, request_body_type if request_body else None, response_type)

        # 处理查询参数和路径参数
        path_params = operation.get('parameters', [])
        query_params = []
        path_param_names = []

        for param in path_params:
            param_in = param.get('in')
            param_name = param.get('name', '')
            param_schema = param.get('schema', {})
            param_type = self.generate_typescript_type(param_schema, f"{function_name}Param")

            if param_in == 'query':
                query_params.append(f"{param_name}: {param_type}")
                has_query_params = True
            elif param_in == 'path':
                path_param_names.append(param_name)
                has_path_params = True

        # 构建函数参数
        params = []
        if has_path_params:
            # 路径参数作为第一个参数
            path_params_type = '{ ' + ', '.join([f'{name}: string | number' for name in path_param_names]) + ' }'
            params.append(f'pathParams: {path_params_type}')

        # 处理请求体和查询参数
        if has_query_params and not request_body:
            # 只有查询参数，没有请求体
            query_params_type = '{ ' + ', '.join(query_params) + ' }'
            params.append(f'params: {query_params_type}')
        elif request_body and has_query_params:
            # 有请求体和查询参数
            if request_body.get('required', False):
                params.append(f'data: {request_type_alias}')
            else:
                params.append(f'data?: {request_type_alias}')
            query_params_type = '{ ' + ', '.join(query_params) + ' }'
            params.append(f'queryParams: {query_params_type}')
        elif request_body:
            # 只有请求体
            if request_body.get('required', False):
                params.append(f'data: {request_type_alias}')
            else:
                params.append(f'data?: {request_type_alias}')

        param_str = ', '.join(params)

        # 构建URL（处理路径参数）
        final_url = path
        if has_path_params:
            # 将 {param} 替换为实际的路径参数值
            url_parts = []
            path_parts = final_url.split('/')
            for part in path_parts:
                if part.startswith('{') and part.endswith('}'):
                    param_name = part[1:-1]
                    url_parts.append(f'${{pathParams.{param_name}}}')
                else:
                    url_parts.append(part)
            final_url = '/'.join(url_parts)

        # 根据HTTP方法选择对应的请求函数
        http_method = method.upper()
        request_function = ''

        if http_method == 'GET':
            if has_query_params:
                request_function = (
                    f"return useGet<{response_type_alias}>('{final_url}', params)"
                )
            else:
                request_function = f"return useGet<{response_type_alias}>('{final_url}')"
        elif http_method == 'POST':
            if has_query_params and not request_body:
                # 只有查询参数的POST请求，使用表单数据方式
                request_function = (
                    "return usePost<" + response_type_alias + ">(\'" + final_url + "\', params, {\n"
                    "  headers: { 'Content-Type': 'application/x-www-form-urlencoded' }\n"
                    "})"
                )
            elif has_query_params and request_body:
                # 有请求体和查询参数
                request_function = (
                    f"return usePost<{response_type_alias}>('{final_url}', data, {{ params: queryParams }})"
                )
            elif request_body:
                # 只有请求体
                request_function = f"return usePost<{response_type_alias}>('{final_url}', data)"
            else:
                # 没有参数
                request_function = f"return usePost<{response_type_alias}>('{final_url}')"
        elif http_method == 'PUT':
            if has_query_params and not request_body:
                # 只有查询参数的PUT请求
                request_function = f"return usePut<{response_type_alias}>('{final_url}', params)"
            elif has_query_params and request_body:
                # 有请求体和查询参数
                request_function = (
                    f"return usePut<{response_type_alias}>('{final_url}', data, {{ params: queryParams }})"
                )
            elif request_body:
                # 只有请求体
                request_function = f"return usePut<{response_type_alias}>('{final_url}', data)"
            else:
                # 没有参数
                request_function = f"return usePut<{response_type_alias}>('{final_url}')"
        elif http_method == 'DELETE':
            if has_query_params and not request_body:
                # 只有查询参数的DELETE请求
                request_function = f"return useDelete<{response_type_alias}>('{final_url}', params)"
            elif has_query_params and request_body:
                # 有请求体和查询参数
                request_function = (
                    f"return useDelete<{response_type_alias}>('{final_url}', data, {{ params: queryParams }})"
                )
            elif request_body:
                # 只有请求体
                request_function = f"return useDelete<{response_type_alias}>('{final_url}', data)"
            else:
                # 没有参数
                request_function = f"return useDelete<{response_type_alias}>('{final_url}')"
        else:
            # 对于其他HTTP方法，使用通用的instance请求
            request_function = (
                "return request.request<" + response_type_alias + ">({\n"
                f"    url: '{final_url}',\n"
                f"    method: '{http_method}',\n"
                f"    {'data,' if 'data' in param_str else ''}\n"
                f"    {'params: queryParams,' if has_query_params else ''}\n"
                "})"
            )

        # 生成函数体
        function_code = f"""
/**
 * {summary}
 */
export const {function_name} = async ({param_str}): Promise<ResponseBody<{response_type_alias}>> => {{
  {request_function};
}};
"""
        return function_name, function_code.strip()

    def _build_function_name(self, module_name: str, path: str, method: str, operation_id: str) -> str:
        # 检查是否存在已生成的映射
        converted_url = self._convert_path_to_url(path)
        existing_key = (method.upper(), converted_url)

        module_key = ''.join(c if c.isalnum() else '_' for c in (module_name or 'Default')).lower()
        seen = self._module_func_names.setdefault(module_key, set())
        global_seen = self._global_func_names

        if existing_key in self.existing_mappings:
            existing_name = self.existing_mappings[existing_key]
            seen.add(existing_name)
            global_seen.add(existing_name)
            return existing_name

        base = (
            self._normalize_identifier(operation_id)
            if operation_id
            and not self._is_sequential_operation_id(operation_id)
            and not self._is_generic_operation_id(operation_id)
            else self._build_short_name(path, method)
        )
        name = base
        if name not in seen and name not in global_seen:
            seen.add(name)
            global_seen.add(name)
            self.existing_mappings[existing_key] = name
            return name
        segs = [s for s in path.strip('/').split('/') if s]
        non_params = [s for s in segs if not (s.startswith('{') and s.endswith('}'))]
        generics = {'api', 'v1', 'v2', 'v3', 'app'}
        tokens = [s for s in non_params if s not in generics]
        action_tokens = {
            'save',
            'delete',
            'remove',
            'list',
            'page',
            'copy',
            'byId',
            'byAlias',
            'search',
            'query',
            'update',
            'detail',
            'info',
            'del',
        }
        action = None
        resource = tokens[-1] if tokens else 'resource'
        parent = tokens[-2] if len(tokens) >= 2 else None
        if tokens:
            last = tokens[-1]
            if last in action_tokens:
                action = last
                resource = tokens[-2] if len(tokens) >= 2 else resource
                parent = tokens[-3] if len(tokens) >= 3 else parent
        method_lower = method.lower()
        if action == 'list':
            prefix = 'list'
        elif action == 'page':
            prefix = 'page'
        elif action == 'save':
            prefix = 'save'
        elif action in {'delete', 'remove', 'del'}:
            prefix = 'delete'
        elif action == 'copy':
            prefix = 'copy'
        elif action in {'byId', 'byAlias', 'detail', 'info'}:
            prefix = 'get'
        else:
            if method_lower == 'get':
                prefix = 'get'
            elif method_lower == 'post':
                prefix = 'query'
            elif method_lower in {'put', 'patch'}:
                prefix = 'update'
            elif method_lower == 'delete':
                prefix = 'remove'
            else:
                prefix = method_lower
        suffix = ''
        if action == 'byId' or ('byId' in segs):
            suffix = 'ById'
        elif action == 'byAlias' or ('byAlias' in segs):
            suffix = 'ByAlias'
        if parent:
            name2 = self._to_camel(f"{prefix}_{parent}_{resource}{suffix}")
            if name2 not in seen and name2 not in global_seen:
                seen.add(name2)
                global_seen.add(name2)
                self.existing_mappings[existing_key] = name2
                return name2
        param_names = [s[1:-1] for s in segs if s.startswith('{') and s.endswith('}')]
        if param_names:
            name3 = self._to_camel(
                f"{prefix}_{resource}_{'By' + ''.join([p.capitalize() for p in param_names])}"
            )
            if name3 not in seen and name3 not in global_seen:
                seen.add(name3)
                global_seen.add(name3)
                self.existing_mappings[existing_key] = name3
                return name3
        last2 = '_'.join(tokens[-2:]) if len(tokens) >= 2 else resource
        name4 = self._to_camel(f"{prefix}_{last2}{suffix}")
        if name4 not in seen and name4 not in global_seen:
            seen.add(name4)
            global_seen.add(name4)
            self.existing_mappings[existing_key] = name4
            return name4
        i = 2
        while f"{name}{i}" in seen or f"{name}{i}" in global_seen:
            i += 1
        final = f"{name}{i}"
        seen.add(final)
        global_seen.add(final)
        self.existing_mappings[existing_key] = final
        return final

    def _is_generic_operation_id(self, op_id: str) -> bool:
        if not op_id:
            return True
        return op_id.lower() in {
            'get',
            'save',
            'update',
            'delete',
            'list',
            'page',
            'detail',
            'info',
            'query',
            'remove',
            'copy',
        }

    def _is_sequential_operation_id(self, op_id: str) -> bool:
        if not op_id:
            return True
        if re.fullmatch(r'\d+', op_id):
            return True
        if re.search(r'[_-][0-9]+$', op_id):
            return True
        if re.fullmatch(r'(get|save|update|delete|list|page|detail)[-_]?\d+', op_id, flags=re.IGNORECASE):
            return True
        return False

    def _normalize_identifier(self, text: str) -> str:
        if not text:
            return 'fn'
        allowed = []
        for ch in text:
            if ch.isalnum() or ch == '_' or ch == '$':
                allowed.append(ch)
            else:
                allowed.append('_')
        ident = ''.join(allowed)
        if ident[0].isdigit():
            ident = '_' + ident
        return ident

    def _to_camel(self, text: str) -> str:
        text = text.replace('/', '_').replace('-', '_')
        parts = [p for p in text.split('_') if p]
        deduped_parts = []
        for part in parts:
            if deduped_parts and deduped_parts[-1].lower() == part.lower():
                continue
            deduped_parts.append(part)
        parts = deduped_parts
        if not parts:
            return 'fn'
        first = parts[0].lower()
        rest_tokens = []
        for part in parts[1:]:
            if not part:
                continue
            part_norm = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', part)
            subparts = [sp for sp in re.split(r'[_\s]+', part_norm) if sp]
            rest_tokens.append(''.join(sp[:1].upper() + sp[1:].lower() for sp in subparts))
        candidate = first + ''.join(rest_tokens)
        return self._normalize_identifier(candidate)

    def _normalize_module_alias(self, module_name: str, used_aliases: Set[str], index: int) -> str:
        ascii_only = ''.join(ch if ch.isalnum() else '_' for ch in module_name)
        ascii_only = ''.join(ch for ch in ascii_only if ch.isascii())
        ascii_only = ascii_only.strip('_')
        if ascii_only:
            alias_base = self._to_camel(ascii_only)
        else:
            alias_base = f'module{index}'
        alias = alias_base
        i = 2
        while alias in used_aliases:
            alias = f'{alias_base}{i}'
            i += 1
        used_aliases.add(alias)
        return alias

    def _build_short_name(self, path: str, method: str) -> str:
        segs = [s for s in path.strip('/').split('/') if s]
        non_params = [s for s in segs if not (s.startswith('{') and s.endswith('}'))]
        generics = {'api', 'v1', 'v2', 'v3', 'app'}
        tokens = [s for s in non_params if s not in generics]

        action_tokens = {
            'save',
            'delete',
            'remove',
            'list',
            'page',
            'copy',
            'byId',
            'byAlias',
            'search',
            'query',
            'update',
            'detail',
            'info',
            'del',
        }
        action = None
        if tokens:
            last = tokens[-1]
            if last in action_tokens:
                action = last
                tokens = tokens[:-1]
            else:
                for act in [
                    'list',
                    'page',
                    'save',
                    'delete',
                    'copy',
                    'byId',
                    'byAlias',
                    'search',
                    'query',
                    'update',
                    'remove',
                    'del',
                ]:
                    if act in tokens:
                        action = act
                        break
        resource = tokens[-1] if tokens else 'resource'

        if resource in {"data", "info", "detail", "group", "list"} and len(tokens) >= 2:
            resource = f"{tokens[-2]}_{resource}"
        method_lower = method.lower()
        if action == 'list':
            prefix = 'list'
        elif action == 'page':
            prefix = 'page'
        elif action in {'save'}:
            prefix = 'save'
        elif action in {'delete', 'remove', 'del'}:
            prefix = 'delete'
        elif action in {'copy'}:
            prefix = 'copy'
        elif action in {'search', 'query'}:
            prefix = 'query'
        elif action in {'update'}:
            prefix = 'update'
        elif action in {'byId', 'byAlias'}:
            prefix = 'get'
        else:
            if method_lower == 'get':
                prefix = 'get'
            elif method_lower == 'post':
                prefix = 'query'
            elif method_lower in {'put', 'patch'}:
                prefix = 'update'
            elif method_lower == 'delete':
                prefix = 'remove'
            else:
                prefix = method_lower
        suffix = ''
        if action == 'byId' or ('byId' in segs):
            suffix = 'ById'
        elif action == 'byAlias' or ('byAlias' in segs):
            suffix = 'ByAlias'
        base = (
            f"{prefix}_{resource}"
            f"{('_' + action) if (action and action not in {'list', 'page', 'save', 'delete', 'remove', 'del', 'copy', 'byId', 'byAlias', 'query', 'update', 'detail', 'info'}) else ''}"
            f"{suffix}"
        )
        return self._to_camel(base)

    def get_response_type(self, operation: Dict[str, Any]) -> str:
        """获取响应数据类型"""
        return_type = 'any'
        responses = operation.get('responses', {})
        success_response = responses.get('200') or responses.get('201') or responses.get('default', {})
        if success_response:
            content = success_response.get('content', {})
            for _content_type, media_type in content.items():
                if 'schema' in media_type:
                    response_schema = media_type['schema']
                    return_type = self.generate_typescript_type(response_schema, "Response")
                    break
        return return_type

    def collect_used_types(self, operation: Dict[str, Any]) -> Set[str]:
        """收集API操作中使用的所有类型"""
        used_types = set()

        # 收集请求体类型
        request_body = operation.get('requestBody')
        if request_body:
            content = request_body.get('content', {})
            for _content_type, media_type in content.items():
                if 'schema' in media_type:
                    request_types = self._collect_types_from_schema(media_type['schema'])
                    used_types.update(request_types)

        # 收集参数类型
        parameters = operation.get('parameters', [])
        for param in parameters:
            if 'schema' in param:
                param_types = self._collect_types_from_schema(param['schema'])
                used_types.update(param_types)

        # 收集响应类型
        responses = operation.get('responses', {})
        success_response = responses.get('200') or responses.get('201') or responses.get('default', {})
        if success_response:
            content = success_response.get('content', {})
            for _content_type, media_type in content.items():
                if 'schema' in media_type:
                    response_types = self._collect_types_from_schema(media_type['schema'])
                    used_types.update(response_types)

        return used_types

    def _collect_types_from_schema(self, schema: Dict[str, Any]) -> Set[str]:
        """从schema中递归收集所有类型引用"""
        types = set()

        if not schema:
            return types

        if '$ref' in schema:
            ref = schema['$ref']
            if ref.startswith('#/components/schemas/') or ref.startswith('#/definitions/'):
                type_name = ref.split('/')[-1]
                types.add(type_name)

                # 递归处理引用的类型定义
                ref_schema = self.resolve_ref(ref)
                if ref_schema:
                    types.update(self._collect_types_from_schema(ref_schema))

        schema_type = schema.get('type')

        if schema_type == 'array':
            items_schema = schema.get('items', {})
            types.update(self._collect_types_from_schema(items_schema))
        elif schema_type == 'object':
            properties = schema.get('properties', {})
            for prop_schema in properties.values():
                types.update(self._collect_types_from_schema(prop_schema))

            # 处理 additionalProperties
            additional_props = schema.get('additionalProperties')
            if additional_props:
                types.update(self._collect_types_from_schema(additional_props))

        return types

    def generate_module_apis(self) -> Dict[str, List[str]]:
        """按模块生成API函数"""
        modules = {}
        module_types = {}  # 存储每个模块使用的类型
        module_operation_types = {}

        for path, methods in self.paths.items():
            for method, operation in methods.items():
                if not isinstance(operation, dict):
                    continue

                tags = operation.get('tags', ['Default'])
                module_name = tags[0] if tags else 'Default'

                if module_name not in modules:
                    modules[module_name] = []
                    module_types[module_name] = set()
                    module_operation_types[module_name] = set()

                try:
                    # 收集该API函数使用的类型
                    used_types = self.collect_used_types(operation)
                    module_types[module_name].update(used_types)

                    function_name, api_function = self.generate_api_function(path, method, operation)
                    modules[module_name].append(api_function)
                    module_operation_types[module_name].add(self._operation_type_name(function_name, 'Response'))
                    if operation.get('requestBody'):
                        module_operation_types[module_name].add(
                            self._operation_type_name(function_name, 'Request')
                        )
                except Exception as exc:
                    print(f"生成API函数失败: {path} {method}, 错误: {exc}")
                    # 生成一个简单的备用函数
                    function_name = operation.get('operationId', f"{method}_{path.replace('/', '_')}")
                    backup_function = f"""
/**
 * {operation.get('summary', '')}
 * 注意: 此函数生成时出错，需要手动完善
 */
export const {function_name} = async (...args: any[]): Promise<ResponseBody<any>> => {{
  throw new Error('API函数需要手动实现');
}};
"""
                    modules[module_name].append(backup_function.strip())

        # 为每个模块生成代码，包括类型导入
        module_codes = {}
        module_aliases = {}
        used_aliases: Set[str] = set()
        for module_name, apis in modules.items():
            if not apis:
                continue

            # 清理模块名，用于文件名
            clean_module_name = ''.join(c if c.isalnum() else '_' for c in module_name)

            # 生成类型导入语句
            type_imports = []
            combined_imports = set(module_types[module_name]) | module_operation_types[module_name]
            for type_name in sorted(combined_imports):
                if type_name in self.generated_interfaces:  # 只导入已生成的接口
                    type_imports.append(type_name)
                elif type_name in module_operation_types[module_name]:
                    type_imports.append(type_name)

            if type_imports:
                import_statement = f"import type {{ {', '.join(type_imports)} }} from './types';\n"

            else:
                import_statement = ""

            module_code = f"""// {module_name} 模块API
// 自动生成，请勿手动修改

import {{ useGet, usePost, usePut, useDelete, request }} from '{self.request_import_path}';
import type {{ ResponseBody }} from '{self.request_import_path}';
{import_statement}
{chr(10).join(apis)}
"""
            module_key = clean_module_name.lower()
            module_codes[module_key] = module_code
            module_aliases[module_key] = self._normalize_module_alias(
                module_name,
                used_aliases,
                len(module_aliases) + 1,
            )

        self._module_counts = {
            module_name: len(apis) for module_name, apis in modules.items() if apis
        }
        self._module_aliases = module_aliases
        return module_codes

    def _convert_path_to_url(self, path: str) -> str:
        """将Swagger路径转换为生成的代码中的URL格式"""
        parts = path.split('/')
        new_parts = []
        for part in parts:
            if part.startswith('{') and part.endswith('}'):
                param_name = part[1:-1]
                new_parts.append(f'${{pathParams.{param_name}}}')
            else:
                new_parts.append(part)
        return '/'.join(new_parts)

    def load_existing_interfaces(self, output_dir: str):
        """加载已生成的接口映射关系"""
        if not os.path.exists(output_dir):
            return

        mapping_path = os.path.join(output_dir, self.mapping_file_name)
        if os.path.exists(mapping_path):
            try:
                with open(mapping_path, 'r', encoding='utf-8') as f:
                    mapping_data = json.load(f)
                for key, func_name in mapping_data.items():
                    method, url = key.split(' ', 1)
                    self.existing_mappings[(method, url)] = func_name
                print(f"✓ 已加载映射文件: {self.mapping_file_name}")
            except Exception as exc:
                print(f"⚠️ 读取映射文件失败: {exc}")

        print(f"🔍 正在扫描现有接口: {output_dir}")
        count = 0

        for root, _, files in os.walk(output_dir):
            for file in files:
                if not file.endswith('.ts') or file == 'types.ts' or file == 'index.ts':
                    continue

                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    # 匹配导出函数
                    matches = re.finditer(
                        r"export\s+const\s+(\w+)\s*=\s*async\s*\(.*?\)\s*:\s*Promise<.*?>\s*=>\s*\{(.*?)\};",
                        content,
                        re.DOTALL,
                    )

                    for match in matches:
                        func_name = match.group(1)
                        body = match.group(2)

                        # 尝试匹配 useXxx 调用
                        use_match = re.search(
                            r"return\s+use(Get|Post|Put|Delete)<.*?>\(\s*['\"]([^'\"]+)['\"]",
                            body,
                        )
                        if use_match:
                            method = use_match.group(1).upper()
                            url = use_match.group(2)
                            self.existing_mappings[(method, url)] = func_name
                            count += 1
                            continue

                        # 尝试匹配 request.request 调用
                        req_match = re.search(r"return\s+request\.request<.*?>\(\s*\{", body)
                        if req_match:
                            url_match = re.search(r"url:\s*['\"]([^'\"]+)['\"]", body)
                            method_match = re.search(r"method:\s*['\"]([^'\"]+)['\"]", body)

                            if url_match and method_match:
                                url = url_match.group(1)
                                method = method_match.group(1).upper()
                                self.existing_mappings[(method, url)] = func_name
                                count += 1

                except Exception as exc:
                    print(f"⚠️ 读取文件 {file} 失败: {exc}")

        print(f"✓ 已加载 {count} 个现有接口映射")

    def _write_mapping_file(self, output_dir: str) -> None:
        mapping_data = {
            f"{method} {url}": func_name for (method, url), func_name in self.existing_mappings.items()
        }
        if not mapping_data:
            return
        mapping_path = os.path.join(output_dir, self.mapping_file_name)
        with open(mapping_path, 'w', encoding='utf-8') as f:
            json.dump(mapping_data, f, ensure_ascii=False, indent=2)
        print(f"✓ 已写入映射文件: {self.mapping_file_name}")

    def _render_operation_types(self) -> str:
        if not self.operation_types:
            return ''
        lines = ["// API 请求/响应类型 (按函数名固定)"]
        for function_name in sorted(self.operation_types.keys()):
            types = self.operation_types[function_name]
            request_type = types.get('request')
            response_type = types.get('response')
            if request_type:
                type_name = self._operation_type_name(function_name, 'Request')
                lines.append(f"export type {type_name} = {request_type}")
            if response_type:
                type_name = self._operation_type_name(function_name, 'Response')
                lines.append(f"export type {type_name} = {response_type}")
            lines.append('')
        return '\n'.join(lines).strip() + '\n'

    def generate_all_code(self, output_dir: str = './generated'):
        """生成所有代码文件"""
        # 加载现有接口映射
        self.load_existing_interfaces(output_dir)

        # 创建输出目录
        os.makedirs(output_dir, exist_ok=True)

        # 生成类型定义文件
        print("📝 正在生成类型定义...")
        interfaces_code = self.generate_all_interfaces()
        with open(os.path.join(output_dir, 'types.ts'), 'w', encoding='utf-8') as f:
            f.write('// 自动生成的类型定义\n')
            f.write('// 由Swagger文档生成，请勿手动修改\n')
            f.write('/* eslint-disable */\n\n')
            f.write(f"import type {{ ResponseBody }} from '{self.request_import_path}';\n\n")
            f.write(interfaces_code)
            if interfaces_code:
                f.write('\n\n')
            f.write(self._render_operation_types())
            print("✓ 生成类型定义文件: types.ts")

        # 按模块生成API文件
        print("🔧 正在生成API函数...")
        module_codes = self.generate_module_apis()

        for filename, module_code in module_codes.items():
            with open(os.path.join(output_dir, f"{filename}.ts"), 'w', encoding='utf-8') as f:
                f.write(module_code)
                print(f"✓ 生成模块文件: {filename}.ts")

        # 生成索引文件
        index_exports = [
            "// 自动生成的API索引",
            "// 由Swagger文档生成",
            "",
            "export * from './types';",
        ]

        module_aliases = getattr(self, '_module_aliases', {})
        for filename in module_codes.keys():
            alias = module_aliases.get(filename, filename)
            # 改为命名空间导出，解决不同模块间函数重名导致的 index.ts 编译错误
            # 使用者可以通过 import { ModuleName } from '@/api' 访问
            # 或者直接 import { func } from '@/api/ModuleName'
            index_exports.append(f"export * as {alias} from './{filename}';")

        with open(os.path.join(output_dir, 'index.ts'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(index_exports))
            print("✓ 生成索引文件: index.ts")

        self._write_mapping_file(output_dir)

        module_counts = getattr(self, '_module_counts', {})
        print("\n🎉 代码生成完成！")
        print(f"📁 输出目录: {os.path.abspath(output_dir)}")
        print(f"📦 生成的模块: {list(module_counts.keys())}")
        print(f"📊 总接口数: {sum(module_counts.values())}")


def load_swagger_from_file(file_path: str) -> Dict[str, Any]:
    """从文件加载Swagger JSON"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            if file_path.endswith('.json'):
                return json.load(f)
            raise ValueError("不支持的文件格式，目前只支持JSON")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"文件不存在: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON解析错误: {exc}") from exc
    except Exception as exc:
        raise Exception(f"读取文件失败: {exc}") from exc


def main():
    parser = argparse.ArgumentParser(description='Swagger文档转前端代码生成器')
    parser.add_argument('--input', '-i', required=True, help='Swagger JSON文件路径')
    parser.add_argument('--output', '-o', default='./src/api', help='输出目录路径')
    parser.add_argument(
        '--request-path',
        '-r',
        default='@/utils/request',
        help='请求工具的导入路径 (默认: @/utils/request)',
    )

    args = parser.parse_args()

    try:
        # 从文件加载Swagger文档
        print(f"📖 正在读取Swagger文档: {args.input}")
        swagger_data = load_swagger_from_file(args.input)

        # 创建生成器
        generator = SwaggerToFrontendGenerator(swagger_data, args.request_path)

        # 生成代码
        generator.generate_all_code(args.output)

    except Exception as exc:
        print(f"❌ 错误: {exc}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


# 直接使用的示例函数

def generate_from_file(
    input_file: str,
    output_dir: str = './src/api',
    base_url: str = None,
    request_path: str = '@/utils/request',
):
    """
    直接从文件生成代码的便捷函数

    Args:
        input_file: Swagger JSON文件路径
        output_dir: 输出目录路径
        base_url: 可选的API基础URL
        request_path: 请求工具的导入路径
    """
    try:
        swagger_data = load_swagger_from_file(input_file)
        generator = SwaggerToFrontendGenerator(swagger_data, request_path)

        if base_url:
            generator.base_url = base_url

        generator.generate_all_code(output_dir)
    except Exception as exc:
        print(f"生成失败: {exc}")


if __name__ == '__main__':
    # 使用方法1: 命令行参数
    # python swagger_generator.py --input swagger.json --output ./src/api
    # exit(main())

    # 使用方法2: 直接调用
    generate_from_file('swagger.json', './src/api', None, '@licos/core')
