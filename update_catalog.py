"""
Comprehensive Australian Used Hardware Reference Price Catalog (65+ Models)
Covers:
- NVIDIA RTX 2000, 3000, 4000, 5000 series
- AMD Radeon RX 6000, 7000, 9000 series
- AMD AM4 & AM5 (Ryzen 3000, 5000, 7000, 9000 & X3D)
- Intel LGA1200, LGA1700 (12th, 13th, 14th Gen) & LGA1851 (Core Ultra 200)
- Motherboards (B450/B550/X570, B650/X670/B850/X870, B660/B760/Z690/Z790, B860/Z890)
- DDR4 & DDR5 RAM Kits (16GB, 32GB, 64GB)
- NVMe / SSD Storage (500GB, 1TB, 2TB, 4TB)
"""
import json
from pathlib import Path

catalog = [
    # --- NVIDIA RTX 2000 SERIES ---
    {"canonical_name": "NVIDIA RTX 2060 / 2060 Super", "category": "GPU", "socket_or_series": "RTX 2000", "avg_used_aud": 185, "good_deal_aud": 145, "steal_aud": 115, "regex_patterns": [r"\brtx\s*2060(?:\s*super|\s*s)?\b", r"\b2060\s*super\b"], "exclude_patterns": ["laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 2070 / 2070 Super", "category": "GPU", "socket_or_series": "RTX 2000", "avg_used_aud": 230, "good_deal_aud": 180, "steal_aud": 145, "regex_patterns": [r"\brtx\s*2070(?:\s*super|\s*s)?\b", r"\b2070\s*super\b"], "exclude_patterns": ["laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 2080 / 2080 Super", "category": "GPU", "socket_or_series": "RTX 2000", "avg_used_aud": 280, "good_deal_aud": 220, "steal_aud": 180, "regex_patterns": [r"\brtx\s*2080(?:\s*super|\s*s)?\b", r"\b2080\s*super\b"], "exclude_patterns": [r"\b2080\s*ti\b", r"\b2080ti\b", "laptop"]},
    {"canonical_name": "NVIDIA RTX 2080 Ti 11GB", "category": "GPU", "socket_or_series": "RTX 2000", "avg_used_aud": 360, "good_deal_aud": 285, "steal_aud": 235, "regex_patterns": [r"\brtx\s*2080\s*ti\b", r"\b2080ti\b"], "exclude_patterns": ["laptop"]},

    # --- NVIDIA RTX 3000 SERIES ---
    {"canonical_name": "NVIDIA RTX 3050", "category": "GPU", "socket_or_series": "RTX 3000", "avg_used_aud": 200, "good_deal_aud": 155, "steal_aud": 125, "regex_patterns": [r"\brtx\s*3050\b"], "exclude_patterns": ["laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 3060 12GB", "category": "GPU", "socket_or_series": "RTX 3000", "avg_used_aud": 280, "good_deal_aud": 225, "steal_aud": 185, "regex_patterns": [r"\brtx\s*3060\b"], "exclude_patterns": [r"\b3060\s*ti\b", r"\b3060ti\b", "laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 3060 Ti 8GB", "category": "GPU", "socket_or_series": "RTX 3000", "avg_used_aud": 340, "good_deal_aud": 275, "steal_aud": 230, "regex_patterns": [r"\brtx\s*3060\s*ti\b", r"\b3060\s*ti\b", r"\b3060ti\b"], "exclude_patterns": ["laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 3070 8GB", "category": "GPU", "socket_or_series": "RTX 3000", "avg_used_aud": 395, "good_deal_aud": 320, "steal_aud": 270, "regex_patterns": [r"\brtx\s*3070\b", r"\b3070\s*8gb\b"], "exclude_patterns": [r"\b3070\s*ti\b", r"\b3070ti\b", "laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 3070 Ti 8GB", "category": "GPU", "socket_or_series": "RTX 3000", "avg_used_aud": 445, "good_deal_aud": 360, "steal_aud": 300, "regex_patterns": [r"\brtx\s*3070\s*ti\b", r"\b3070\s*ti\b", r"\b3070ti\b"], "exclude_patterns": ["laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 3080 10GB/12GB", "category": "GPU", "socket_or_series": "RTX 3000", "avg_used_aud": 580, "good_deal_aud": 475, "steal_aud": 400, "regex_patterns": [r"\brtx\s*3080\b", r"\b3080\s*(?:10gb|12gb)\b"], "exclude_patterns": [r"\b3080\s*ti\b", r"\b3080ti\b", "laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 3080 Ti 12GB", "category": "GPU", "socket_or_series": "RTX 3000", "avg_used_aud": 680, "good_deal_aud": 560, "steal_aud": 475, "regex_patterns": [r"\brtx\s*3080\s*ti\b", r"\b3080\s*ti\b", r"\b3080ti\b"], "exclude_patterns": ["laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 3090 24GB", "category": "GPU", "socket_or_series": "RTX 3000", "avg_used_aud": 960, "good_deal_aud": 780, "steal_aud": 660, "regex_patterns": [r"\brtx\s*3090\b", r"\b3090\s*24gb\b"], "exclude_patterns": [r"\b3090\s*ti\b", r"\b3090ti\b"]},
    {"canonical_name": "NVIDIA RTX 3090 Ti 24GB", "category": "GPU", "socket_or_series": "RTX 3000", "avg_used_aud": 1080, "good_deal_aud": 880, "steal_aud": 750, "regex_patterns": [r"\brtx\s*3090\s*ti\b", r"\b3090\s*ti\b", r"\b3090ti\b"], "exclude_patterns": []},

    # --- NVIDIA RTX 4000 SERIES ---
    {"canonical_name": "NVIDIA RTX 4060 8GB", "category": "GPU", "socket_or_series": "RTX 4000", "avg_used_aud": 360, "good_deal_aud": 295, "steal_aud": 250, "regex_patterns": [r"\brtx\s*4060\b"], "exclude_patterns": [r"\b4060\s*ti\b", r"\b4060ti\b", "laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 4060 Ti (8GB/16GB)", "category": "GPU", "socket_or_series": "RTX 4000", "avg_used_aud": 485, "good_deal_aud": 395, "steal_aud": 335, "regex_patterns": [r"\brtx\s*4060\s*ti\b", r"\b4060\s*ti\b", r"\b4060ti\b"], "exclude_patterns": ["laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 4070 12GB", "category": "GPU", "socket_or_series": "RTX 4000", "avg_used_aud": 680, "good_deal_aud": 560, "steal_aud": 480, "regex_patterns": [r"\brtx\s*4070\b"], "exclude_patterns": [r"\b4070\s*super\b", r"\b4070\s*ti\b", r"\b4070ti\b", "laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 4070 Super 12GB", "category": "GPU", "socket_or_series": "RTX 4000", "avg_used_aud": 810, "good_deal_aud": 670, "steal_aud": 580, "regex_patterns": [r"\brtx\s*4070\s*super\b", r"\b4070\s*super\b", r"\b4070super\b"], "exclude_patterns": [r"\bti\b", "laptop"]},
    {"canonical_name": "NVIDIA RTX 4070 Ti 12GB", "category": "GPU", "socket_or_series": "RTX 4000", "avg_used_aud": 960, "good_deal_aud": 790, "steal_aud": 690, "regex_patterns": [r"\brtx\s*4070\s*ti\b", r"\b4070\s*ti\b", r"\b4070ti\b"], "exclude_patterns": [r"super", "laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 4070 Ti Super 16GB", "category": "GPU", "socket_or_series": "RTX 4000", "avg_used_aud": 1120, "good_deal_aud": 920, "steal_aud": 800, "regex_patterns": [r"\b4070\s*ti\s*super\b", r"\b4070ti\s*super\b"], "exclude_patterns": ["laptop"]},
    {"canonical_name": "NVIDIA RTX 4080 / 4080 Super 16GB", "category": "GPU", "socket_or_series": "RTX 4000", "avg_used_aud": 1380, "good_deal_aud": 1120, "steal_aud": 960, "regex_patterns": [r"\brtx\s*4080\b", r"\b4080\s*super\b", r"\b4080super\b"], "exclude_patterns": ["laptop", "mobile"]},
    {"canonical_name": "NVIDIA RTX 4090 24GB", "category": "GPU", "socket_or_series": "RTX 4000", "avg_used_aud": 2450, "good_deal_aud": 1980, "steal_aud": 1720, "regex_patterns": [r"\brtx\s*4090\b"], "exclude_patterns": ["laptop", "mobile"]},

    # --- NVIDIA RTX 5000 SERIES ---
    {"canonical_name": "NVIDIA RTX 5060 / 5060 Ti", "category": "GPU", "socket_or_series": "RTX 5000", "avg_used_aud": 620, "good_deal_aud": 500, "steal_aud": 430, "regex_patterns": [r"\brtx\s*5060(?:\s*ti)?\b", r"\b5060\s*ti\b"], "exclude_patterns": ["laptop"]},
    {"canonical_name": "NVIDIA RTX 5070 12GB", "category": "GPU", "socket_or_series": "RTX 5000", "avg_used_aud": 890, "good_deal_aud": 730, "steal_aud": 630, "regex_patterns": [r"\brtx\s*5070\b"], "exclude_patterns": [r"\b5070\s*ti\b", r"\b5070ti\b", "laptop"]},
    {"canonical_name": "NVIDIA RTX 5070 Ti 16GB", "category": "GPU", "socket_or_series": "RTX 5000", "avg_used_aud": 1180, "good_deal_aud": 960, "steal_aud": 830, "regex_patterns": [r"\brtx\s*5070\s*ti\b", r"\b5070\s*ti\b", r"\b5070ti\b"], "exclude_patterns": ["laptop"]},
    {"canonical_name": "NVIDIA RTX 5080 16GB", "category": "GPU", "socket_or_series": "RTX 5000", "avg_used_aud": 1680, "good_deal_aud": 1380, "steal_aud": 1180, "regex_patterns": [r"\brtx\s*5080\b"], "exclude_patterns": ["laptop"]},
    {"canonical_name": "NVIDIA RTX 5090 32GB", "category": "GPU", "socket_or_series": "RTX 5000", "avg_used_aud": 3300, "good_deal_aud": 2700, "steal_aud": 2350, "regex_patterns": [r"\brtx\s*5090\b"], "exclude_patterns": ["laptop"]},

    # --- AMD RADEON RX 6000 SERIES ---
    {"canonical_name": "AMD Radeon RX 6600 / 6600 XT / 6650 XT", "category": "GPU", "socket_or_series": "RX 6000", "avg_used_aud": 235, "good_deal_aud": 185, "steal_aud": 150, "regex_patterns": [r"\brx\s*66[05]0(?:\s*xt)?\b", r"\b6600\s*xt\b", r"\b6650\s*xt\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Radeon RX 6700 XT / 6750 XT 12GB", "category": "GPU", "socket_or_series": "RX 6000", "avg_used_aud": 360, "good_deal_aud": 290, "steal_aud": 240, "regex_patterns": [r"\brx\s*67[05]0\s*xt\b", r"\b6700\s*xt\b", r"\b6700xt\b", r"\b6750\s*xt\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Radeon RX 6800 16GB", "category": "GPU", "socket_or_series": "RX 6000", "avg_used_aud": 450, "good_deal_aud": 365, "steal_aud": 310, "regex_patterns": [r"\brx\s*6800\b"], "exclude_patterns": [r"\b6800\s*xt\b", r"\b6800xt\b"]},
    {"canonical_name": "AMD Radeon RX 6800 XT 16GB", "category": "GPU", "socket_or_series": "RX 6000", "avg_used_aud": 520, "good_deal_aud": 420, "steal_aud": 360, "regex_patterns": [r"\brx\s*6800\s*xt\b", r"\b6800\s*xt\b", r"\b6800xt\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Radeon RX 6900 XT / 6950 XT 16GB", "category": "GPU", "socket_or_series": "RX 6000", "avg_used_aud": 640, "good_deal_aud": 520, "steal_aud": 440, "regex_patterns": [r"\brx\s*69[05]0\s*xt\b", r"\b6900\s*xt\b", r"\b6900xt\b", r"\b6950\s*xt\b"], "exclude_patterns": []},

    # --- AMD RADEON RX 7000 & 9000 SERIES ---
    {"canonical_name": "AMD Radeon RX 7600 / 7600 XT", "category": "GPU", "socket_or_series": "RX 7000", "avg_used_aud": 320, "good_deal_aud": 255, "steal_aud": 215, "regex_patterns": [r"\brx\s*7600(?:\s*xt)?\b"], "exclude_patterns": ["ryzen"]},
    {"canonical_name": "AMD Radeon RX 7700 XT 12GB", "category": "GPU", "socket_or_series": "RX 7000", "avg_used_aud": 480, "good_deal_aud": 390, "steal_aud": 330, "regex_patterns": [r"\brx\s*7700\s*xt\b", r"\b7700\s*xt\b", r"\b7700xt\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Radeon RX 7800 XT 16GB", "category": "GPU", "socket_or_series": "RX 7000", "avg_used_aud": 620, "good_deal_aud": 510, "steal_aud": 435, "regex_patterns": [r"\brx\s*7800\s*xt\b", r"\b7800\s*xt\b", r"\b7800xt\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Radeon RX 7900 GRE 16GB", "category": "GPU", "socket_or_series": "RX 7000", "avg_used_aud": 720, "good_deal_aud": 590, "steal_aud": 500, "regex_patterns": [r"\brx\s*7900\s*gre\b", r"\b7900\s*gre\b", r"\b7900gre\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Radeon RX 7900 XT 20GB", "category": "GPU", "socket_or_series": "RX 7000", "avg_used_aud": 880, "good_deal_aud": 720, "steal_aud": 620, "regex_patterns": [r"\brx\s*7900\s*xt\b", r"\b7900\s*xt\b", r"\b7900xt\b"], "exclude_patterns": [r"\b7900\s*xtx\b", r"\b7900xtx\b"]},
    {"canonical_name": "AMD Radeon RX 7900 XTX 24GB", "category": "GPU", "socket_or_series": "RX 7000", "avg_used_aud": 1150, "good_deal_aud": 940, "steal_aud": 800, "regex_patterns": [r"\brx\s*7900\s*xtx\b", r"\b7900\s*xtx\b", r"\b7900xtx\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Radeon RX 9070 16GB", "category": "GPU", "socket_or_series": "RX 9000", "avg_used_aud": 780, "good_deal_aud": 640, "steal_aud": 540, "regex_patterns": [r"\brx\s*9070\b"], "exclude_patterns": [r"\b9070\s*xt\b", r"\b9070xt\b"]},
    {"canonical_name": "AMD Radeon RX 9070 XT 16GB", "category": "GPU", "socket_or_series": "RX 9000", "avg_used_aud": 940, "good_deal_aud": 770, "steal_aud": 660, "regex_patterns": [r"\brx\s*9070\s*xt\b", r"\b9070\s*xt\b", r"\b9070xt\b"], "exclude_patterns": []},

    # --- AMD AM4 CPUs ---
    {"canonical_name": "AMD Ryzen 5 3600 / 3600X", "category": "CPU", "socket_or_series": "AM4", "avg_used_aud": 75, "good_deal_aud": 55, "steal_aud": 40, "regex_patterns": [r"\bryzen\s*5\s*3600x?\b", r"\br5\s*3600x?\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Ryzen 5 5600 / 5600X", "category": "CPU", "socket_or_series": "AM4", "avg_used_aud": 140, "good_deal_aud": 110, "steal_aud": 85, "regex_patterns": [r"\bryzen\s*5\s*5600x?\b", r"\b5600x\b", r"\br5\s*5600x?\b"], "exclude_patterns": [r"\b5600g\b", r"\b5600x3d\b"]},
    {"canonical_name": "AMD Ryzen 7 5700X / 5800X", "category": "CPU", "socket_or_series": "AM4", "avg_used_aud": 210, "good_deal_aud": 165, "steal_aud": 135, "regex_patterns": [r"\bryzen\s*7\s*5[78]00x\b", r"\b5700x\b", r"\b5800x\b"], "exclude_patterns": [r"x3d"]},
    {"canonical_name": "AMD Ryzen 7 5700X3D", "category": "CPU", "socket_or_series": "AM4", "avg_used_aud": 270, "good_deal_aud": 215, "steal_aud": 180, "regex_patterns": [r"\b5700x3d\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Ryzen 7 5800X3D", "category": "CPU", "socket_or_series": "AM4", "avg_used_aud": 360, "good_deal_aud": 290, "steal_aud": 240, "regex_patterns": [r"\b5800x3d\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Ryzen 9 5900X / 5950X", "category": "CPU", "socket_or_series": "AM4", "avg_used_aud": 340, "good_deal_aud": 270, "steal_aud": 225, "regex_patterns": [r"\b5900x\b", r"\b5950x\b"], "exclude_patterns": []},

    # --- AMD AM5 CPUs (Ryzen 7000 & 9000) ---
    {"canonical_name": "AMD Ryzen 5 7500F / 7600 / 7600X", "category": "CPU", "socket_or_series": "AM5", "avg_used_aud": 240, "good_deal_aud": 190, "steal_aud": 155, "regex_patterns": [r"\bryzen\s*5\s*7[56]00[fx]?\b", r"\b7500f\b", r"\b7600x\b", r"\br5\s*7600x?\b"], "exclude_patterns": [r"\brx\s*7600\b", "radeon"]},
    {"canonical_name": "AMD Ryzen 7 7700 / 7700X", "category": "CPU", "socket_or_series": "AM5", "avg_used_aud": 340, "good_deal_aud": 275, "steal_aud": 225, "regex_patterns": [r"\bryzen\s*7\s*7700x?\b", r"\b7700x\b", r"\br7\s*7700x?\b"], "exclude_patterns": [r"\brx\s*7700\b", "radeon"]},
    {"canonical_name": "AMD Ryzen 7 7800X3D", "category": "CPU", "socket_or_series": "AM5", "avg_used_aud": 540, "good_deal_aud": 440, "steal_aud": 375, "regex_patterns": [r"\b7800x3d\b", r"\b7800\s*x3d\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Ryzen 9 7900X / 7900X3D / 7950X / 7950X3D", "category": "CPU", "socket_or_series": "AM5", "avg_used_aud": 620, "good_deal_aud": 495, "steal_aud": 420, "regex_patterns": [r"\b7900x(?:3d)?\b", r"\b7950x(?:3d)?\b"], "exclude_patterns": ["radeon", "rx"]},
    {"canonical_name": "AMD Ryzen 5 9600X", "category": "CPU", "socket_or_series": "AM5", "avg_used_aud": 330, "good_deal_aud": 265, "steal_aud": 220, "regex_patterns": [r"\b9600x\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Ryzen 7 9700X", "category": "CPU", "socket_or_series": "AM5", "avg_used_aud": 430, "good_deal_aud": 345, "steal_aud": 295, "regex_patterns": [r"\b9700x\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Ryzen 7 9800X3D", "category": "CPU", "socket_or_series": "AM5", "avg_used_aud": 690, "good_deal_aud": 570, "steal_aud": 490, "regex_patterns": [r"\b9800x3d\b", r"\b9800\s*x3d\b"], "exclude_patterns": []},
    {"canonical_name": "AMD Ryzen 9 9900X / 9950X / 9950X3D", "category": "CPU", "socket_or_series": "AM5", "avg_used_aud": 820, "good_deal_aud": 670, "steal_aud": 570, "regex_patterns": [r"\b9900x(?:3d)?\b", r"\b9950x(?:3d)?\b"], "exclude_patterns": []},

    # --- INTEL LGA1700 & LGA1851 CPUs ---
    {"canonical_name": "Intel Core i5-12400 / 12400F / 12600K / 12600KF", "category": "CPU", "socket_or_series": "LGA1700", "avg_used_aud": 175, "good_deal_aud": 135, "steal_aud": 105, "regex_patterns": [r"\bi5[-\s]*12[46]00k?f?\b", r"\b12400f?\b", r"\b12600k?f?\b"], "exclude_patterns": []},
    {"canonical_name": "Intel Core i7-12700K / 12700KF / 12700F", "category": "CPU", "socket_or_series": "LGA1700", "avg_used_aud": 280, "good_deal_aud": 220, "steal_aud": 180, "regex_patterns": [r"\bi7[-\s]*12700k?f?\b", r"\b12700k?f?\b"], "exclude_patterns": ["12700h"]},
    {"canonical_name": "Intel Core i5-13600K / 14600K / KF", "category": "CPU", "socket_or_series": "LGA1700", "avg_used_aud": 290, "good_deal_aud": 230, "steal_aud": 190, "regex_patterns": [r"\bi5[-\s]*1[34]600k?f?\b", r"\b1[34]600k?f?\b"], "exclude_patterns": []},
    {"canonical_name": "Intel Core i7-13700K / 14700K / KF", "category": "CPU", "socket_or_series": "LGA1700", "avg_used_aud": 410, "good_deal_aud": 330, "steal_aud": 275, "regex_patterns": [r"\bi7[-\s]*1[34]700k?f?\b", r"\b1[34]700k?f?\b"], "exclude_patterns": ["13700h", "14700hx"]},
    {"canonical_name": "Intel Core i9-13900K / 14900K / KF / KS", "category": "CPU", "socket_or_series": "LGA1700", "avg_used_aud": 560, "good_deal_aud": 450, "steal_aud": 380, "regex_patterns": [r"\bi9[-\s]*1[34]900k?[fs]?\b", r"\b1[34]900k?[fs]?\b"], "exclude_patterns": ["13900h", "14900hx"]},
    {"canonical_name": "Intel Core Ultra 5 245K / KF", "category": "CPU", "socket_or_series": "LGA1851", "avg_used_aud": 350, "good_deal_aud": 280, "steal_aud": 235, "regex_patterns": [r"\bultra\s*5\s*245k?f?\b", r"\b245k?f?\b"], "exclude_patterns": []},
    {"canonical_name": "Intel Core Ultra 7 265K / KF", "category": "CPU", "socket_or_series": "LGA1851", "avg_used_aud": 490, "good_deal_aud": 395, "steal_aud": 335, "regex_patterns": [r"\bultra\s*7\s*265k?f?\b", r"\b265k?f?\b"], "exclude_patterns": []},
    {"canonical_name": "Intel Core Ultra 9 285K", "category": "CPU", "socket_or_series": "LGA1851", "avg_used_aud": 740, "good_deal_aud": 600, "steal_aud": 510, "regex_patterns": [r"\bultra\s*9\s*285k\b", r"\b285k\b"], "exclude_patterns": []},

    # --- MOTHERBOARDS ---
    {"canonical_name": "AM4 Motherboard (B450 / B550 / X570)", "category": "Motherboard", "socket_or_series": "AM4", "avg_used_aud": 135, "good_deal_aud": 100, "steal_aud": 75, "regex_patterns": [r"\bb450[a-z0-9-]*\b", r"\bb550[a-z0-9-]*\b", r"\bx570[a-z0-9-]*\b"], "exclude_patterns": []},
    {"canonical_name": "AM5 Motherboard (B650 / X670 / B850 / X870)", "category": "Motherboard", "socket_or_series": "AM5", "avg_used_aud": 230, "good_deal_aud": 175, "steal_aud": 140, "regex_patterns": [r"\bb650e?\b", r"\bx670e?\b", r"\bb850\b", r"\bx870e?\b"], "exclude_patterns": []},
    {"canonical_name": "LGA1700 Motherboard (B660 / B760 / Z690 / Z790)", "category": "Motherboard", "socket_or_series": "LGA1700", "avg_used_aud": 195, "good_deal_aud": 150, "steal_aud": 115, "regex_patterns": [r"\bb660\b", r"\bb760\b", r"\bz690\b", r"\bz790\b"], "exclude_patterns": []},
    {"canonical_name": "LGA1851 Motherboard (Z890 / B860)", "category": "Motherboard", "socket_or_series": "LGA1851", "avg_used_aud": 290, "good_deal_aud": 220, "steal_aud": 175, "regex_patterns": [r"\bz890\b", r"\bb860\b"], "exclude_patterns": []},

    # --- RAM & STORAGE ---
    {"canonical_name": "16GB DDR4 RAM Kit", "category": "RAM", "socket_or_series": "DDR4", "avg_used_aud": 45, "good_deal_aud": 35, "steal_aud": 25, "regex_patterns": [r"\b16\s*gb\s*(?:[0-9]{4}\s*mhz\s*)?ddr4\b", r"\bddr4\s*16\s*gb\b"], "exclude_patterns": []},
    {"canonical_name": "32GB DDR4 RAM Kit", "category": "RAM", "socket_or_series": "DDR4", "avg_used_aud": 75, "good_deal_aud": 55, "steal_aud": 40, "regex_patterns": [r"\b32\s*gb\s*(?:[0-9]{4}\s*mhz\s*)?ddr4\b", r"\bddr4\s*32\s*gb\b"], "exclude_patterns": []},
    {"canonical_name": "32GB DDR5 RAM Kit", "category": "RAM", "socket_or_series": "DDR5", "avg_used_aud": 125, "good_deal_aud": 95, "steal_aud": 75, "regex_patterns": [r"\b32\s*gb\s*(?:[0-9]{4}\s*mhz\s*)?ddr5\b", r"\bddr5\s*32\s*gb\b"], "exclude_patterns": []},
    {"canonical_name": "64GB DDR5 RAM Kit", "category": "RAM", "socket_or_series": "DDR5", "avg_used_aud": 230, "good_deal_aud": 180, "steal_aud": 145, "regex_patterns": [r"\b64\s*gb\s*(?:[0-9]{4}\s*mhz\s*)?ddr5\b", r"\bddr5\s*64\s*gb\b"], "exclude_patterns": []},
    {"canonical_name": "1TB NVMe / SSD", "category": "Storage", "socket_or_series": "NVMe", "avg_used_aud": 75, "good_deal_aud": 55, "steal_aud": 40, "regex_patterns": [r"\b1\s*tb\s*(?:nvme|m\.2|ssd)\b", r"\b(?:nvme|m\.2|ssd)\s*1\s*tb\b"], "exclude_patterns": []},
    {"canonical_name": "2TB NVMe / SSD", "category": "Storage", "socket_or_series": "NVMe", "avg_used_aud": 145, "good_deal_aud": 110, "steal_aud": 85, "regex_patterns": [r"\b2\s*tb\s*(?:nvme|m\.2|ssd)\b", r"\b(?:nvme|m\.2|ssd)\s*2\s*tb\b"], "exclude_patterns": []}
]

Path(__file__).with_name("catalog_seed.json").write_text(json.dumps(catalog, indent=2))
print(f"Wrote {len(catalog)} parts to catalog_seed.json")
