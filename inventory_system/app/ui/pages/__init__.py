"""One module per sidebar page. Real, data-driven pages (Dashboard,
Inventory, Sales, Purchases) call their Service via AsyncContentArea on a
background thread; the rest (Products, Warehouses, Suppliers, Customers,
Reports, Users, Settings) are honest PlaceholderPage empty states because
their backing entities/services don't exist yet — see docs/architecture.md.
"""
