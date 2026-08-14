"""Pydantic DTOs — the only objects that cross the GUI <-> Service boundary.

The GUI never sees a repository row, an openpyxl cell value, or an ORM
instance directly; it builds one of these from its Tk state and gets one
back from a Service.
"""
