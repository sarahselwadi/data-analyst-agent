"""
Azure Application Insights OpenTelemetry Integration for Azure AI Agents

This module provides centralized telemetry configuration for:
- Azure Monitor OpenTelemetry Distro (Azure Functions, HTTP requests)
- Logfire instrumentation (Azure AI Agents SDK tracing)
- Custom spans for business logic tracking

Based on Microsoft's recommended practices for Azure AI Foundry observability.
"""

import os
import logging
from typing import Optional

# Initialize telemetry flag to prevent multiple configurations
_telemetry_configured = False

def configure_telemetry(
    connection_string: Optional[str] = None,
    service_name: str = "dataanalyst-func",
    enable_content_recording: bool = False
) -> bool:
    """
    Configure comprehensive telemetry for Azure AI Agents application.
    
    Args:
        connection_string: Application Insights connection string
        service_name: Service name for telemetry identification
        enable_content_recording: Whether to record AI conversation content (may contain PII)
    
    Returns:
        bool: True if telemetry was successfully configured
    """
    global _telemetry_configured
    
    if _telemetry_configured:
        logging.info("Telemetry already configured, skipping...")
        return True
    
    try:
        # Check if Application Insights is enabled via environment variable
        app_insights_enabled = os.getenv("ENABLE_APPLICATION_INSIGHTS", "true").lower() == "true"
        
        if not app_insights_enabled:
            logging.info("Application Insights disabled via ENABLE_APPLICATION_INSIGHTS environment variable.")
            return False
        
        # Get connection string from parameter or environment
        conn_str = connection_string or os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
        
        if not conn_str:
            logging.warning("No Application Insights connection string provided. Telemetry disabled.")
            return False
        
        # Parse connection string to validate format
        if not _validate_connection_string(conn_str):
            logging.error("Invalid Application Insights connection string format")
            return False
        
        # Configure environment variables for optimal telemetry
        _configure_environment_variables(service_name, enable_content_recording)
        
        # Configure Azure Monitor OpenTelemetry Distro
        success_azure_monitor = _configure_azure_monitor(conn_str)
        
        # Configure Logfire for AI Agents instrumentation
        success_logfire = _configure_logfire_instrumentation(service_name)
        
        if success_azure_monitor or success_logfire:
            _telemetry_configured = True
            logging.info(f"✅ Telemetry configured successfully for service: {service_name}")
            logging.info(f"   - Azure Monitor: {'✅' if success_azure_monitor else '❌'}")
            logging.info(f"   - AI Agents Tracing: {'✅' if success_logfire else '❌'}")
            return True
        else:
            logging.error("Failed to configure any telemetry providers")
            return False
            
    except Exception as e:
        logging.error(f"Failed to configure telemetry: {str(e)}", exc_info=True)
        return False

def _validate_connection_string(connection_string: str) -> bool:
    """Validate Application Insights connection string format"""
    required_components = ["InstrumentationKey=", "IngestionEndpoint="]
    return all(component in connection_string for component in required_components)

def _configure_environment_variables(service_name: str, enable_content_recording: bool):
    """Configure OpenTelemetry environment variables for optimal performance"""
    
    # Service identification
    os.environ["OTEL_SERVICE_NAME"] = service_name
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = f"service.name={service_name},service.version=1.0.0"
    
    # AI content recording (for debugging - may contain PII)
    if enable_content_recording:
        os.environ["AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED"] = "true"
        logging.warning("⚠️ AI content recording enabled - may capture PII in telemetry")
    
    # Performance optimizations
    os.environ["OTEL_BSP_SCHEDULE_DELAY"] = "5000"  # Batch span processor delay (5s)
    os.environ["OTEL_BLRP_SCHEDULE_DELAY"] = "5000"  # Batch log record processor delay (5s)
    os.environ["OTEL_TRACES_SAMPLER_ARG"] = "1.0"    # 100% sampling for now
    
    # Disable specific instrumentations if needed (comma-separated)
    # os.environ["OTEL_PYTHON_DISABLED_INSTRUMENTATIONS"] = "urllib3,requests"

def _configure_azure_monitor(connection_string: str) -> bool:
    """Configure Azure Monitor OpenTelemetry Distro"""
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        
        # Configure Azure Monitor with connection string
        configure_azure_monitor(
            connection_string=connection_string,
            disable_offline_storage=False,  # Enable offline storage for reliability
        )
        
        logging.info("🔧 Azure Monitor OpenTelemetry configured")
        return True
        
    except ImportError:
        logging.warning("azure-monitor-opentelemetry not available. Install: pip install azure-monitor-opentelemetry")
        return False
    except Exception as e:
        logging.error(f"Failed to configure Azure Monitor: {str(e)}")
        return False

def _configure_logfire_instrumentation(service_name: str) -> bool:
    """Configure Logfire for Azure AI Agents SDK instrumentation"""
    try:
        import logfire
        
        # Configure Logfire to send to Azure Monitor (not Logfire cloud)
        logfire.configure(
            service_name=service_name,
            send_to_logfire=False,  # Send to Azure Monitor instead
            distributed_tracing=True,
            console=False,  # Disable console logging in production
        )
        
        # Instrument OpenAI Agents SDK
        logfire.instrument_openai_agents()
        
        # Optional: Instrument additional libraries
        # logfire.instrument_requests()  # For HTTP requests tracing
        # logfire.instrument_asyncio()   # For async operations tracing
        
        logging.info("🤖 Logfire AI Agents instrumentation configured")
        return True
        
    except ImportError:
        logging.warning("logfire not available. Install: pip install logfire")
        return False
    except Exception as e:
        logging.error(f"Failed to configure Logfire instrumentation: {str(e)}")
        return False

def create_custom_span(name: str, operation_type: str = "custom"):
    """
    Create a custom OpenTelemetry span for manual instrumentation.
    
    Usage:
        with create_custom_span("file_upload", "io"):
            # Your code here
            pass
    """
    try:
        from opentelemetry import trace
        
        tracer = trace.get_tracer(__name__)
        span = tracer.start_span(name)
        span.set_attribute("operation.type", operation_type)
        return span
    except ImportError:
        # Return a no-op context manager if OpenTelemetry is not available
        from contextlib import nullcontext
        return nullcontext()

def add_span_attributes(**attributes):
    """Add attributes to the current active span"""
    try:
        from opentelemetry import trace
        
        current_span = trace.get_current_span()
        if current_span and current_span.is_recording():
            for key, value in attributes.items():
                current_span.set_attribute(key, str(value))
    except ImportError:
        pass

def record_exception(exception: Exception, span_name: str = "error"):
    """Record an exception in the current span or create a new error span"""
    try:
        from opentelemetry import trace
        
        current_span = trace.get_current_span()
        if current_span and current_span.is_recording():
            current_span.record_exception(exception)
            current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(exception)))
        else:
            # Create a new span for the error
            tracer = trace.get_tracer(__name__)
            with tracer.start_span(span_name) as error_span:
                error_span.record_exception(exception)
                error_span.set_status(trace.Status(trace.StatusCode.ERROR, str(exception)))
    except ImportError:
        pass

# Convenience function for quick telemetry setup
def setup_telemetry_from_env() -> bool:
    """
    Setup telemetry using environment variables.
    
    Expected environment variables:
    - APPLICATIONINSIGHTS_CONNECTION_STRING: Application Insights connection string
    - OTEL_SERVICE_NAME: Service name (optional, defaults to 'dataanalyst-func')
    - AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED: Enable content recording (optional)
    - OTEL_RESOURCE_ATTRIBUTES: Additional resource attributes (optional)
    """
    service_name = os.getenv("OTEL_SERVICE_NAME", "dataanalyst-func")
    content_recording = os.getenv("AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED", "false").lower() == "true"
    
    return configure_telemetry(
        service_name=service_name,
        enable_content_recording=content_recording
    )

def trace_ai_operation(operation_name: str, model_name: str = None, session_id: str = None, thread_id: str = None, **kwargs):
    """
    Create a span specifically for AI operations following Azure AI Agents SDK patterns with proper correlation.
    
    Args:
        operation_name: Name of the AI operation (e.g., "chat_completion", "assistant_run")
        model_name: AI model being used
        session_id: User session ID for correlation
        thread_id: Azure AI Foundry thread ID for operation correlation
        **kwargs: Additional attributes for the span
    """
    try:
        from opentelemetry import trace
        
        tracer = trace.get_tracer(__name__)
        span = tracer.start_span(f"ai.{operation_name}")
        
        # Set standard AI operation attributes
        span.set_attribute("ai.operation.name", operation_name)
        if model_name:
            span.set_attribute("ai.model.name", model_name)
            
        # Set Azure AI Foundry correlation attributes
        if session_id:
            span.set_attribute("ai.session.id", session_id)
            span.set_attribute("session_id", session_id)  # For Application Insights custom dimensions
        if thread_id:
            span.set_attribute("ai.thread.id", thread_id)
            span.set_attribute("thread_id", thread_id)  # For Application Insights custom dimensions
        
        # Add custom attributes
        for key, value in kwargs.items():
            span.set_attribute(f"ai.{key}", str(value))
            
        return span
    except ImportError:
        from contextlib import nullcontext
        return nullcontext()

def create_correlation_context(session_id: str = None, thread_id: str = None, operation_id: str = None):
    """
    Create a correlation context for grouping related telemetry following Azure AI Foundry patterns.
    
    This function sets up logging scope with session and thread correlation for Azure Application Insights.
    Based on Microsoft's recommended practices for Azure AI Foundry observability.
    
    Args:
        session_id: User session identifier
        thread_id: Azure AI Foundry thread identifier
        operation_id: Custom operation identifier
        
    Returns:
        Context manager for correlation scope
    """
    try:
        # First try to use logging scopes for Application Insights correlation
        import logging
        from typing import Dict, Any
        
        correlation_data: Dict[str, Any] = {}
        
        if session_id:
            correlation_data["session_id"] = session_id
        if thread_id:
            correlation_data["thread_id"] = thread_id
            correlation_data["ai_foundry_thread_id"] = thread_id  # For Azure AI Foundry correlation
        if operation_id:
            correlation_data["operation_id"] = operation_id
            
        # Create a logging scope with correlation data
        return CorrelationScope(correlation_data)
        
    except ImportError:
        from contextlib import nullcontext
        return nullcontext()

class CorrelationScope:
    """
    Context manager for creating correlation scope in Application Insights.
    
    This ensures all logs within the scope include session_id and thread_id
    for proper grouping in Azure Application Insights.
    """
    
    def __init__(self, correlation_data: dict):
        self.correlation_data = correlation_data
        self.logger = logging.getLogger("azure.ai.foundry")
        self._span = None
        
    def __enter__(self):
        try:
            from opentelemetry import trace
            
            # Create a span with correlation attributes
            tracer = trace.get_tracer("azure.ai.foundry.correlation")
            self._span = tracer.start_span("correlation_scope")
            
            # Add all correlation data as span attributes
            for key, value in self.correlation_data.items():
                self._span.set_attribute(key, str(value))
                
            # Log correlation context establishment
            correlation_msg = " | ".join([f"{k}={v}" for k, v in self.correlation_data.items()])
            self.logger.info(f"🔗 Correlation context established: {correlation_msg}")
            
            return self
            
        except ImportError:
            # Fallback to simple logging if OpenTelemetry is not available
            correlation_msg = " | ".join([f"{k}={v}" for k, v in self.correlation_data.items()])
            self.logger.info(f"🔗 Correlation context (simple): {correlation_msg}")
            return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._span:
            try:
                from opentelemetry import trace
                if exc_type:
                    self._span.record_exception(exc_val)
                    self._span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc_val)))
                self._span.end()
            except ImportError:
                pass
            
    def log_info(self, message: str, **kwargs):
        """Log info message with correlation context"""
        enriched_kwargs = {**self.correlation_data, **kwargs}
        extra_info = " | ".join([f"{k}={v}" for k, v in enriched_kwargs.items() if k not in message])
        self.logger.info(f"{message} | {extra_info}")
        
    def log_error(self, message: str, **kwargs):
        """Log error message with correlation context"""
        enriched_kwargs = {**self.correlation_data, **kwargs}
        extra_info = " | ".join([f"{k}={v}" for k, v in enriched_kwargs.items() if k not in message])
        self.logger.error(f"{message} | {extra_info}")
        
    def log_warning(self, message: str, **kwargs):
        """Log warning message with correlation context"""
        enriched_kwargs = {**self.correlation_data, **kwargs}
        extra_info = " | ".join([f"{k}={v}" for k, v in enriched_kwargs.items() if k not in message])
        self.logger.warning(f"{message} | {extra_info}")

if __name__ == "__main__":
    # Test telemetry configuration
    print("Testing telemetry configuration...")
    
    # Example connection string (replace with your actual one)
    test_connection_string = "InstrumentationKey=54f136f0-612c-4616-8298-05ab78992cc2;IngestionEndpoint=https://westeurope-5.in.applicationinsights.azure.com/;LiveEndpoint=https://westeurope.livediagnostics.monitor.azure.com/;ApplicationId=7b0e846a-205c-48d6-9c63-d5e12fd3a452"
    
    success = configure_telemetry(
        connection_string=test_connection_string,
        service_name="dataanalyst-func-test",
        enable_content_recording=False
    )
    
    if success:
        print("✅ Telemetry configuration test successful!")
        
        # Test custom span creation
        with create_custom_span("test_operation", "test"):
            add_span_attributes(test_attribute="test_value", operation="telemetry_test")
            print("📊 Custom span created and attributes added")
    else:
        print("❌ Telemetry configuration test failed!")
