"""Model-independent Windows VM control interface."""
__version__='0.3.0'

def __getattr__(name):
    if name=='Controller':
        from .sdk import Controller
        return Controller
    raise AttributeError(name)
