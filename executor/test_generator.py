"""
Test Data Generator and Validator
"""
import random
import json
import logging
from typing import Any, Dict
from dataclasses import dataclass

from orchestrator.models import Workflow

logger = logging.getLogger(__name__)

@dataclass
class ValidationResult:
    passed: bool
    error: str = ""
    details: Dict[str, Any] = None

def generate_test_input(workflow: Workflow) -> Dict[str, Any]:
    """
    Generate synthetic test data based on workflow trigger definitions.
    """
    input_data = {}
    
    # Analyze nodes to find triggers
    nodes = workflow.nodes
    triggers = [n for n in nodes if n.get('type', '').endswith('trigger') or n.get('type') == 'manual_trigger']
    
    for trigger in triggers:
        # Simple heuristic specific generation
        t_type = trigger.get('type')
        if t_type == 'manual_trigger':
            # Look for schema in config?
            config = trigger.get('data', {}).get('config', {})
            # If config has 'fields' definition, generate matching data
            # For now, generate generic sample
            input_data['text'] = "Sample test text"
            input_data['number'] = random.randint(1, 100)
            input_data['boolean'] = True
            
        elif t_type == 'webhook_trigger':
            input_data['body'] = {"message": "Test webhook payload", "id": 123}
            input_data['headers'] = {"Content-Type": "application/json"}
            input_data['query'] = {"test": "true"}
            
        elif t_type == 'email_trigger':
            input_data['subject'] = "Test Email Subject"
            input_data['from'] = "test@example.com"
            input_data['body'] = "This is a test email body."
    
    return input_data

def validate_test_result(
    result: Dict[str, Any],
    expected_schema: Dict[str, Any] | None = None,
    timeout_limit_ms: int = 30000
) -> ValidationResult:
    """
    Validate execution results against expectations.
    """
    # 1. Check Execution Status
    # Result here is usually the output_data from ExecutionLog specific to completion
    # But we might need the full ExecutionLog status passed in, 
    # or 'result' implies the dictionary returned by orchestrator.execute() which is handle?
    # Usually we get the final output dictionary.
    
    # Assuming result is the final output data dict.
    
    # If expected_schema provided, validate against it
    if expected_schema:
        # Simple key check for now
        for key, value_type in expected_schema.items():
            if key not in result:
                return ValidationResult(False, f"Missing expected output key: {key}")
            # Type check could be added here
            
    return ValidationResult(True, details={"matches_schema": True})
