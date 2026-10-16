"""Watch Oracle subnet: wholesale floor price prediction for luxury watch references."""

__version__ = "0.3.0"

_version_split = __version__.split(".")
# Passed as version_key when setting weights. Bump __version__ on any change
# that makes validators incompatible with each other.
__spec_version__ = (
    (1000 * int(_version_split[0]))
    + (10 * int(_version_split[1]))
    + (1 * int(_version_split[2]))
)
