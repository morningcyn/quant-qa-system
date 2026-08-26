# 助理（投顾）会话质检工具 - 后端配置常量与默认评分模板
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包运行（PyInstaller）：数据目录 = exe 同级 data/（可写、可备份、随包迁移）；
    # 静态前端资源位于 PyInstaller 解压目录 _MEIPASS（只读）。
    ROOT_DIR = Path(sys.executable).resolve().parent
    FRONTEND_DIR = Path(getattr(sys, "_MEIPASS", ROOT_DIR)) / "frontend"
else:
    # 开发运行：项目根目录
    ROOT_DIR = Path(__file__).resolve().parent.parent
    FRONTEND_DIR = ROOT_DIR / "frontend"

DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "app.db"

APP_VERSION = "1.0.0"

# 大模型调用超时（秒）
LLM_TIMEOUT = 180.0
LLM_CONNECT_TIMEOUT = 10.0
TEST_TIMEOUT = 20.0
# 质检接口客户端最长等待
INSPECTION_TIMEOUT = 200.0

# 默认质检模板（权重按《打分机制》图片：D端55 + S端45；总分<59 黄灯熔断）
# standard(标准) / newbie(新人) / vip(高净值) 三套，评分尺度说明与锚点不同，权重一致。
DEFAULT_TEMPLATES = {
    "standard": {
        "name": "标准服务模板",
        "scale_note": "按标准投顾服务要求评分，各维度均衡考察。",
        "yellow_threshold": 59,
        "d": {
            "d1": {
                "name": "情绪转化",
                "max": 10,
                "weight": 10,
                "anchors": "对比客户开场与收尾情绪走向：明显变好/轻微变好/基本持平/轻微恶化/明显恶化。",
                "ratings": "明显变好9-10分；轻微变好6-8分；基本持平4-5分；轻微恶化2-3分；明显恶化0-1分。",
            },
            "d2": {
                "name": "画像匹配",
                "max": 15,
                "weight": 15,
                "anchors": "识别客户画像类型（焦虑型/冷漠型/强势型/理性型/犹豫型），判断应对方式是否与画像匹配。",
                "ratings": "完全匹配12-15分；基本匹配8-11分；部分匹配4-7分；不匹配0-3分。",
            },
            "d3": {
                "name": "诉求穿透",
                "max": 15,
                "weight": 15,
                "anchors": "穿透表面诉求识别深层诉求（资金安全/情绪安抚/收益预期/关系信任），方案是否命中底层诉求。",
                "ratings": "看懂底层13-15分；看懂部分8-12分；只看表面3-7分；没看懂0-2分。",
            },
            "d4": {
                "name": "预期超越",
                "max": 15,
                "weight": 15,
                "anchors": "是否主动预判客户衍生问题并提前给出答案、是否把掌控感交还客户（给选项/给时间节点）。",
                "ratings": "全面超越13-15分；有所超越8-12分；基本达标3-7分；未达预期0-2分。",
            },
        },
        "s": {
            "s1": {
                "name": "情绪维度",
                "max": 20,
                "sub_items": {
                    "empathy": {"name": "共情", "max": 4},
                    "customized": {"name": "定制化", "max": 5},
                    "direct": {"name": "直接", "max": 4},
                    "no_conflict": {"name": "无冲突", "max": 5},
                    "vent_guide": {"name": "宣泄引导", "max": 2},
                },
            },
            "s2": {
                "name": "问题闭环",
                "max": 15,
                "sub_items": {
                    "completeness": {"name": "完整性", "max": 4},
                    "structure": {"name": "结构化", "max": 4},
                    "next_step": {"name": "下一步动作", "max": 3},
                    "follow_up": {"name": "跟进承诺", "max": 4},
                },
            },
            "s3": {
                "name": "专业供给",
                "max": 10,
                "sub_items": {
                    "logic": {"name": "逻辑", "max": 4},
                    "explain_why": {"name": "讲原因", "max": 3},
                    "decision_ownership": {"name": "决策归属", "max": 3},
                },
            },
        },
    },
    "newbie": {
        "name": "新人辅导模板",
        "scale_note": "针对新人：侧重基础服务动作是否到位（问题闭环、下一步动作），情绪共情与专业深度要求适当放宽。",
        "yellow_threshold": 59,
        "d": {
            "d1": {
                "name": "情绪转化",
                "max": 10,
                "weight": 10,
                "anchors": "对比客户开场与收尾情绪走向，新人做到不恶化即为基本合格。",
                "ratings": "明显变好9-10分；轻微变好6-8分；基本持平4-5分；轻微恶化2-3分；明显恶化0-1分。",
            },
            "d2": {
                "name": "画像匹配",
                "max": 15,
                "weight": 15,
                "anchors": "识别客户画像类型（焦虑型/冷漠型/强势型/理性型/犹豫型），新人能识别并保持态度匹配即可。",
                "ratings": "完全匹配12-15分；基本匹配8-11分；部分匹配4-7分；不匹配0-3分。",
            },
            "d3": {
                "name": "诉求穿透",
                "max": 15,
                "weight": 15,
                "anchors": "表面诉求回应是否完整，能否至少理解一层深层诉求。",
                "ratings": "看懂底层13-15分；看懂部分8-12分；只看表面3-7分；没看懂0-2分。",
            },
            "d4": {
                "name": "预期超越",
                "max": 15,
                "weight": 15,
                "anchors": "是否给出清晰的下一步动作与时间节点；新人达到基本达标即可，不苛求衍生问题预判。",
                "ratings": "全面超越13-15分；有所超越8-12分；基本达标3-7分；未达预期0-2分。",
            },
        },
        "s": {
            "s1": {
                "name": "情绪维度",
                "max": 20,
                "sub_items": {
                    "empathy": {"name": "共情", "max": 4},
                    "customized": {"name": "定制化", "max": 5},
                    "direct": {"name": "直接", "max": 4},
                    "no_conflict": {"name": "无冲突", "max": 5},
                    "vent_guide": {"name": "宣泄引导", "max": 2},
                },
            },
            "s2": {
                "name": "问题闭环",
                "max": 15,
                "sub_items": {
                    "completeness": {"name": "完整性", "max": 4},
                    "structure": {"name": "结构化", "max": 4},
                    "next_step": {"name": "下一步动作", "max": 3},
                    "follow_up": {"name": "跟进承诺", "max": 4},
                },
            },
            "s3": {
                "name": "专业供给",
                "max": 10,
                "sub_items": {
                    "logic": {"name": "逻辑", "max": 4},
                    "explain_why": {"name": "讲原因", "max": 3},
                    "decision_ownership": {"name": "决策归属", "max": 3},
                },
            },
        },
    },
    "vip": {
        "name": "高净值客户模板",
        "scale_note": "针对高净值客户：定制化与情绪管理要求更高，专业方案必须体现资产配置视角与长期信任经营。",
        "yellow_threshold": 59,
        "d": {
            "d1": {
                "name": "情绪转化",
                "max": 10,
                "weight": 10,
                "anchors": "高净值客户情绪敏感度高，对比开场与收尾情绪走向，重点关注是否建立了被重视感。",
                "ratings": "明显变好9-10分；轻微变好6-8分；基本持平4-5分；轻微恶化2-3分；明显恶化0-1分。",
            },
            "d2": {
                "name": "画像匹配",
                "max": 15,
                "weight": 15,
                "anchors": "识别客户画像类型（焦虑型/冷漠型/强势型/理性型/犹豫型）并匹配高净值客户的尊享服务姿态。",
                "ratings": "完全匹配12-15分；基本匹配8-11分；部分匹配4-7分；不匹配0-3分。",
            },
            "d3": {
                "name": "诉求穿透",
                "max": 15,
                "weight": 15,
                "anchors": "穿透表面诉求识别深层诉求（资产安全/家族传承/隐私尊重/收益预期），方案是否体现资产配置视角。",
                "ratings": "看懂底层13-15分；看懂部分8-12分；只看表面3-7分；没看懂0-2分。",
            },
            "d4": {
                "name": "预期超越",
                "max": 15,
                "weight": 15,
                "anchors": "是否主动预判衍生问题、提供专属方案与备选路径、把决策掌控感交还客户。",
                "ratings": "全面超越13-15分；有所超越8-12分；基本达标3-7分；未达预期0-2分。",
            },
        },
        "s": {
            "s1": {
                "name": "情绪维度",
                "max": 20,
                "sub_items": {
                    "empathy": {"name": "共情", "max": 4},
                    "customized": {"name": "定制化", "max": 5},
                    "direct": {"name": "直接", "max": 4},
                    "no_conflict": {"name": "无冲突", "max": 5},
                    "vent_guide": {"name": "宣泄引导", "max": 2},
                },
            },
            "s2": {
                "name": "问题闭环",
                "max": 15,
                "sub_items": {
                    "completeness": {"name": "完整性", "max": 4},
                    "structure": {"name": "结构化", "max": 4},
                    "next_step": {"name": "下一步动作", "max": 3},
                    "follow_up": {"name": "跟进承诺", "max": 4},
                },
            },
            "s3": {
                "name": "专业供给",
                "max": 10,
                "sub_items": {
                    "logic": {"name": "逻辑", "max": 4},
                    "explain_why": {"name": "讲原因", "max": 3},
                    "decision_ownership": {"name": "决策归属", "max": 3},
                },
            },
        },
    },
}

TEMPLATE_TYPE_NAMES = {key: val["name"] for key, val in DEFAULT_TEMPLATES.items()}
