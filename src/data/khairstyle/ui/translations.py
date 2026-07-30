"""English glosses for the K-Hairstyle Korean labels shown in the browsing UI.

These are display aids only. They are NOT ground truth and play no role in the
benchmark; the salon attributes remain mining-only signal. Many
category names are Korean salon/perm trade names that don't have a crisp English
equivalent — glosses lean descriptive. Unknown values fall back to the raw
Korean in the UI.
"""

# category_id -> English label (Korean shown alongside in the UI).
CATEGORY_EN = {
    "0001": "Side Part",
    "0002": "Other Men's Styles",
    "0003": "Other Layered",
    "0004": "Other Women's Styles",
    "0005": "Men's Regular Short",
    "0006": "Dandy Cut",
    "0007": "Loop Perm",
    "0008": "Regent (Pompadour)",
    "0009": "Leaf Cut",
    "0010": "Misty Perm",
    "0011": "Body Perm",
    "0012": "Baby Perm",
    "0013": "Bonnie Perm",
    "0014": "Bob",
    "0015": "Build Perm",
    "0016": "Soft Two-Block Dandy",
    "0017": "Short Bob",
    "0018": "Shadow Cut",
    "0019": "Comma Hair",
    "0020": "Spin Swallow Perm",
    "0021": "See-Through Dandy",
    "0022": "As Perm",
    "0023": "Air Perm",
    "0024": "Women's Regular Short",
    "0025": "One-Length",
    "0026": "One-Block Dandy",
    "0027": "Tassel Cut",
    "0028": "Pomade",
    "0029": "Pleats Perm",
    "0030": "Hush Cut",
    "0031": "Hippie Perm",
}

# Human-readable labels for the index's attribute columns.
FIELD_LABELS = {
    "basestyle": "Base style",
    "basestyle_type": "Length class",
    "length": "Length",
    "curl": "Curl pattern",
    "bang": "Bangs / fringe",
    "loss": "Hair loss",
    "side": "Sides",
    "partition": "Parting",
    "color": "Color",
    "sex": "Sex",
    "age": "Age",
    "vertical": "Camera height",
    "horizontal": "Camera azimuth",
    "front": "Frontal",
    "exceptional": "Exceptional",
    "before_after": "Before/After",
    "hair_width": "Strand width",
    "water_repellency": "Water-repellency",
    "natural_curl": "Natural curl",
    "damage": "Damage",
    "melanin_color": "Melanin",
    "black_colorize": "Black-dyed",
    "patch_test": "Patch test",
    "decolorize_history": "Bleach history",
    "user_satisfied": "Client rating",
    "designer_satisfied": "Stylist rating",
    "color_rgb": "Mean hair RGB",
    "collect_type": "Collected via",
    "device": "Device",
}

# Per-field controlled-vocabulary value glosses. Free-text fields (e.g. color)
# get a partial dictionary; anything missing falls back to the Korean.
VALUE_EN = {
    "sex": {"남": "Male", "여": "Female"},
    "basestyle_type": {"단": "Short", "장": "Long"},
    "length": {
        "남자": "Men's", "여숏": "Women's short", "단발": "Bob-length",
        "중발": "Medium", "장발": "Long",
    },
    "curl": {
        "X": "None (straight)", "S": "S-curl", "SS": "Strong S-curl",
        "S3": "Triple S-curl", "SC": "S+C blend", "C": "C-curl",
        "CC": "Strong C-curl", "CS": "C+S blend", "J": "J-curl",
    },
    "loss": {
        "탈모아님": "No loss", "초기탈모": "Early-stage", "부분탈모": "Partial",
        "탈모": "Significant",
    },
    "side": {"원블럭": "One-block", "투블럭": "Two-block", "해당없음": "N/A"},
    "hair_width": {"굵음": "Thick", "보통": "Medium", "얇음": "Thin"},
    "water_repellency": {"무": "No", "유": "Yes"},
    "black_colorize": {"무": "No", "유": "Yes"},
    "patch_test": {"무": "No", "유": "Yes"},
    "natural_curl": {
        "생머리": "Straight", "반곱슬": "Wavy", "곱슬": "Curly", "강곱슬": "Very curly",
    },
    "damage": {
        "버진": "Virgin", "건강모": "Healthy", "손상모": "Damaged", "극손상모": "Severe",
    },
    "melanin_color": {"적멜라닌": "Red (pheomelanin)", "황멜라닌": "Yellow (eumelanin)"},
    "vertical": {"상": "High", "중": "Mid", "하": "Low"},
    "front": {1: "Frontal", 0: "Non-frontal", "1": "Frontal", "0": "Non-frontal"},
    "before_after": {"before": "Before", "after": "After", "none": "—"},
    "bang": {
        "해당없음": "N/A", "풀뱅": "Full fringe", "시스루": "See-through",
        "처피뱅": "Choppy", "살짝 넘긴스타일": "Slightly swept",
        "페이스라인에 통합": "Blended to face line",
        "기타(남자 내림머리)": "Other (men's down)",
    },
    "partition": {"가르마없음": "No part"},
    "color": {
        "블랙": "Black", "자연갈색": "Natural brown", "황갈색": "Yellow-brown",
        "애쉬브라운": "Ash brown", "적갈색": "Red-brown", "명갈색": "Light brown",
        "암갈색": "Dark brown", "흑갈색": "Black-brown", "금색": "Blonde",
        "적색": "Red", "오렌지": "Orange", "회색": "Gray", "백색": "White",
        "녹색": "Green", "청색": "Blue", "보라색": "Purple", "분홍색": "Pink",
        "갈색": "Brown",
    },
    "collect_type": {"진솔": "Jinsol", "두쏠": "Dussol", "크라우드픽": "Crowdpic"},
}


def category_en(category_id, fallback=""):
    return CATEGORY_EN.get(category_id, fallback)


def value_en(field, value):
    if value is None:
        return None
    m = VALUE_EN.get(field, {})
    if value in m:
        return m[value]
    if str(value) in m:
        return m[str(value)]
    return value
