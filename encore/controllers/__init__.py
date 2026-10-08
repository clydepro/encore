"""Thin request handlers that map HTTP/HTMX endpoints onto application services.

Controllers receive dependencies through their constructors, translate requests
into service calls and render fragments. They never open databases and never
contain domain logic (AIG 4, SAPRS 11.10).
"""
