Drop charted cable geometry here as GeoJSON and it is loaded automatically as
CHARTED-class routes, before any network source is consulted. This is how you
lift detections above LOW confidence and how you make the tool independent of
any remote cable layer.

    ogr2ogr -f GeoJSON fi_cblsub.geojson /path/to/ENC_ROOT/FI5xxxxx.000 CBLSUB

Free ENC sources: NOAA (US), Traficom (FI), DMA (DK), Sjofartsverket (SE),
Transpordiamet (EE). UKHO ADMIRALTY is paid. Kingfisher publish UK cable
awareness charts for the fishing industry.
