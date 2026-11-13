from hypothesis import settings

# A per-example deadline turns a busy machine into a flaky failure. Inherits whichever profile
# Hypothesis picked, so CI keeps its derandomized, database-free one.
settings.register_profile("gate", deadline=None)
settings.load_profile("gate")
