from django.test import TestCase
from .handlers.base import BaseNodeHandler, FieldConfig, FieldType, HandleDef, NodeSchema
from .handlers.registry import NodeRegistry

class TransformationNode(BaseNodeHandler):
    node_type = "test_transform"
    name = "Test Transform"
    category = "test"
    description = "Test Description"
    fields = [
        FieldConfig(name="field1", label="Field 1", field_type=FieldType.STRING, default="value1")
    ]

    async def execute(self, input_data, config, context):
        return {}

class NodeSerializationTests(TestCase):
    def test_schema_aliases(self):
        """Verify that schema serialization uses correct frontend aliases"""
        handler = TransformationNode()
        schema = handler.get_schema()
        dump = schema.model_dump(by_alias=True)
        
        # Check NodeSchema aliases
        self.assertIn("displayName", dump)
        self.assertEqual(dump["displayName"], "Test Transform")
        self.assertNotIn("name", dump)  # Should be aliased
        
        self.assertIn("nodeType", dump)
        self.assertEqual(dump["nodeType"], "test_transform")
        self.assertNotIn("node_type", dump)
        
        # Check FieldConfig aliases
        field = dump["fields"][0]
        self.assertIn("id", field)
        self.assertEqual(field["id"], "field1")
        self.assertNotIn("name", field)
        
        self.assertIn("type", field)
        self.assertEqual(field["type"], "string")
        self.assertNotIn("field_type", field)
        
        self.assertIn("defaultValue", field)
        self.assertEqual(field["defaultValue"], "value1")
        self.assertNotIn("default", field)

    def test_registry_output(self):
        """Verify registry returns aliased dicts"""
        registry = NodeRegistry.get_instance()
        # registry.register(TransformationNode) # Might already be clear or reused
        # We can just check existing nodes since we updated the code
        
        schemas = registry.get_all_schemas()
        if not schemas:
             registry.register(TransformationNode)
             schemas = registry.get_all_schemas()
             
        first = schemas[0]
        self.assertIn("displayName", first)
        self.assertIn("nodeType", first)
        
        # Check fields
        if first["fields"]:
            field = first["fields"][0]
            self.assertIn("id", field)
            self.assertIn("type", field)
