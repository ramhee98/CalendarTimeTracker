"""
AI Configuration Management
This module handles loading and managing AI-related configuration settings.
"""
import json
import os
import shutil
import streamlit as st
from typing import Dict, Any, Optional

def load_ai_config(config_path: str = "ai_config.json") -> Dict[str, Any]:
    """
    Load AI configuration from JSON file.
    If the config file doesn't exist, it will try to copy from the template file.
    
    Args:
        config_path: Path to the configuration file
        
    Returns:
        Dictionary containing AI configuration settings
    """
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            # Try to copy from template file
            template_path = config_path + ".sample"
            if os.path.exists(template_path):
                try:
                    shutil.copy2(template_path, config_path)
                    st.success(f"✅ Created {config_path} from template file.")
                    with open(config_path, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except Exception as copy_error:
                    st.warning(f"Could not copy template file: {copy_error}. Using default configuration.")
            else:
                st.info(f"No {config_path} or {template_path} found. Using default configuration.")
            
            # Return default configuration if file doesn't exist and template copy failed
            return get_default_ai_config()
    except Exception as e:
        st.warning(f"Error loading AI config: {e}. Using default configuration.")
        return get_default_ai_config()

def get_default_ai_config() -> Dict[str, Any]:
    """
    Get default AI configuration from template file.
    
    Returns:
        Default configuration dictionary loaded from ai_config.json.sample
    """
    template_path = "ai_config.json.sample"
    
    # Load from template file
    if os.path.exists(template_path):
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            st.error(f"Error loading template file {template_path}: {e}")
            return {}
    else:
        st.error(f"Template file {template_path} not found. Please ensure it exists.")
        return {}

def get_system_prompt(config: Dict[str, Any], prompt_type: str = "default") -> str:
    """
    Get system prompt from configuration.
    
    Args:
        config: AI configuration dictionary
        prompt_type: Type of prompt to retrieve ("default" or custom prompt name)
        
    Returns:
        System prompt string
    """
    ai_prompts = config.get("ai_prompts", {})
    
    if prompt_type == "default":
        return ai_prompts.get("default_system_prompt", get_default_ai_config()["ai_prompts"]["default_system_prompt"])
    else:
        custom_prompts = ai_prompts.get("custom_prompts", {})
        return custom_prompts.get(prompt_type, ai_prompts.get("default_system_prompt", ""))

def get_available_prompt_types(config: Dict[str, Any]) -> list:
    """
    Get list of available prompt types.
    
    Args:
        config: AI configuration dictionary
        
    Returns:
        List of available prompt types
    """
    custom_prompts = config.get("ai_prompts", {}).get("custom_prompts", {})
    prompt_types = ["default"] + list(custom_prompts.keys())
    return prompt_types

def save_ai_config(config: Dict[str, Any], config_path: str = "ai_config.json") -> bool:
    """
    Save AI configuration to JSON file.
    
    Args:
        config: Configuration dictionary to save
        config_path: Path to save the configuration file
        
    Returns:
        True if successful, False otherwise
    """
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        st.error(f"Error saving AI config: {e}")
        return False

def format_prompt_template(prompt_template: str, group_label: str, time_group: str) -> str:
    """
    Format prompt template with dynamic values.
    
    Args:
        prompt_template: Template string with placeholders
        group_label: Label for the grouping (e.g., "Calendar", "Category")
        time_group: Time grouping period (e.g., "day", "week", "month")
        
    Returns:
        Formatted prompt string
    """
    try:
        return prompt_template.format(
            group_label=group_label.lower(),
            time_group=time_group
        )
    except KeyError as e:
        st.warning(f"Missing placeholder in prompt template: {e}")
        return prompt_template
    except Exception as e:
        st.warning(f"Error formatting prompt template: {e}")
        return prompt_template

def ensure_config_exists(config_path: str = "ai_config.json") -> bool:
    """
    Ensure AI configuration file exists by creating it from template if needed.
    
    Args:
        config_path: Path to the configuration file
        
    Returns:
        True if config exists or was created successfully, False otherwise
    """
    if os.path.exists(config_path):
        return True
    
    template_path = config_path + ".sample"
    if os.path.exists(template_path):
        try:
            shutil.copy2(template_path, config_path)
            return True
        except Exception:
            return False
    
    return False
