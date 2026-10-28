"""Small output validator for the JSON-schema vocabulary used by cognition."""
import math


def validate(value, schema):
    for union in ('anyOf', 'oneOf'):
        if union in schema:
            matches = 0
            for branch in schema[union]:
                try:
                    validate(value, branch)
                    matches += 1
                except ValueError:
                    pass
            if not matches or union == 'oneOf' and matches != 1:
                raise ValueError('schema_union')
            return
    if 'const' in schema and value != schema['const']:
        raise ValueError('schema_const')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('schema_enum')
    kind = schema.get('type')
    kinds = kind if isinstance(kind, list) else [kind]
    types = {'object':dict,'array':list,'string':str,'integer':int,'boolean':bool,'null':type(None)}
    if kind and not any(type(value) is types.get(k) or k=='number' and type(value) in (int,float) for k in kinds):
        raise ValueError('schema_type')
    if type(value) in (int,float) and not math.isfinite(value):
        raise ValueError('schema_number')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if set(schema.get('required', []))-set(value):
            raise ValueError('schema_required')
        if schema.get('additionalProperties') is False and set(value)-set(properties):
            raise ValueError('schema_extra')
        for key in value.keys() & properties.keys():
            validate(value[key], properties[key])
    elif isinstance(value, list):
        if not schema.get('minItems',0) <= len(value) <= schema.get('maxItems',float('inf')):
            raise ValueError('schema_array')
        for item in value:
            validate(item, schema.get('items', {}))
    elif isinstance(value, str):
        if not schema.get('minLength',0) <= len(value) <= schema.get('maxLength',float('inf')):
            raise ValueError('schema_string')
