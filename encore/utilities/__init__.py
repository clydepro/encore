"""Small, stateless helpers shared across Encore.

Utilities must stay genuinely small and side-effect free. Anything with a policy
decision belongs in a domain service, not here (AEP 9: avoid large utility
classes).
"""
