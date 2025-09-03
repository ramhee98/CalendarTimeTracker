# Streamlit Performance Configuration
# This file contains configuration helpers for better performance

import streamlit as st
from functools import wraps

def optimize_page_load(func):
    """Decorator to optimize page loading with session state management"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Initialize page-specific session state
        page_name = func.__name__
        session_key = f"{page_name}_initialized"
        
        if session_key not in st.session_state:
            st.session_state[session_key] = False
            
        # Add page loading indicator
        if not st.session_state[session_key]:
            with st.spinner(f"Loading {page_name.replace('_', ' ').title()}..."):
                result = func(*args, **kwargs)
                st.session_state[session_key] = True
                return result
        else:
            return func(*args, **kwargs)
    
    return wrapper

def clear_page_cache(page_name=None):
    """Clear cache for specific page or all pages"""
    if page_name:
        session_key = f"{page_name}_initialized"
        if session_key in st.session_state:
            del st.session_state[session_key]
    else:
        # Clear all page cache
        keys_to_delete = [key for key in st.session_state.keys() if key.endswith('_initialized')]
        for key in keys_to_delete:
            del st.session_state[key]
    
    # Clear Streamlit data cache
    st.cache_data.clear()

def add_refresh_button(label="🔄 Refresh", help_text="Refresh page data", key=None):
    """Add a standardized refresh button to pages"""
    if st.button(label, help=help_text, key=key):
        clear_page_cache()
        st.rerun()
