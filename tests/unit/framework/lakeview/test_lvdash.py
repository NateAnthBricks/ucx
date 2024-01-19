import json

from databricks.labs.ucx.framework.lakeview.model import Dashboard

# ChartEncodingMapWithSingleXY
# 	x: SingleFieldAxisEncoding
# 		fieldName, scale (required)
# 	y: SingleFieldAxisEncoding
# 		fieldName, scale (required)
# 	color: ColorFieldEncoding
# 		fieldName, scale (required)
# 	label: LabelEncoding
# 		show (required)
#
# ChartEncodingMapWithMultiX
# 	x: MultiFieldAxisEncoding
# 		fields, scale (required)
# 	y: SingleFieldAxisEncoding
# 		fieldName, scale (required)
# 	color: ColorEncodingForMultiSeries
# 		-
# 	label: LabelEncoding
# 		show (required)
#
# ChartEncodingMapWithMultiY
# 	x: SingleFieldAxisEncoding
# 		fieldName, scale (required)
# 	y: MultiFieldAxisEncoding
# 		fields, scale (required)
# 	color: ColorEncodingForMultiSeries
# 		-
# 	label: LabelEncoding
# 		show (required)


def is_assignable():
    pass


def test_lvdash():
    with open("/Users/serge.smertin/Downloads/Databricks Labs GitHub telemetry.lvdash (1).json") as f:
        raw = json.load(f)
        lvdash = Dashboard.from_dict(raw)
        assert lvdash is not None
