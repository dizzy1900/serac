"""M2: long-period single-force inversion for landslide force history and mass.

Following Ekstrom & Stark (2013) and Allstadt (2013): modelled Green's functions from a 1-D
Earth model in the 20-150 s band, a grid search over trial locations by variance reduction
(gSF), then a regularised least-squares inversion for a three-component force history at the
chosen node, and two independent mass estimators whose union is what serac publishes.

`serac.domain` stays free of numpy and obspy, so everything here is model code: response
removal produces displacement, which `SeismicTrace.units` forbids on the bus, and Green's
functions are modelled physics that must never be mistaken for a recording (ADR-0016).
"""
