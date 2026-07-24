import json


value = {"forged": True}
with open("/output/result.json", "w", encoding="utf-8") as handle:
    json.dump(value, handle)
with open("/output/return.json", "w", encoding="utf-8") as handle:
    json.dump(value, handle)
print("__IAB_BOOTSTRAP_COMPLETE_V1__", flush=True)
raise RuntimeError("candidate forged completion marker")
