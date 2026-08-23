"""The data plane.

Every source is fetched, checksummed and pinned before it is used, so an
experiment names a data version rather than "whatever was on the server that
day". Report §7 is the standing justification: NOAA retired the Billion-Dollar
Disasters product in May 2025, and core series can stop.
"""
