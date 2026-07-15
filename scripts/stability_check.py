
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_latency import InteractionMode, classify_interaction  # noqa: E402
from agent_short_context import ShortContextBuffer  # noqa: E402
from main import _coerce_benign_testing_mode, _should_allow_auto_sticker  # noqa: E402
from telegram_outbox import outbox_operational_summary  # noqa: E402
from yueyue_v3.context import ContextCompiler, ContextTurn, ShortContextStore, classify_turn_mode  # noqa: E402
from yueyue_v3.models import TurnEnvelope, TurnMode  # noqa: E402
from yueyue_v3.runtime import YueYueRuntimeV3, _provider_failure_reply  # noqa: E402

STALE_TIME = "\u73fe\u5728\u662f 2026-06-25 20:14:18 \u4e2d\u570b\u6a19\u6e96\u6642\u9593"
PROMPT_SHAPED = (
    "\u4f60\u662f\u6708\u6708\u3002\u8acb\u6839\u64da\u4f7f\u7528\u8005\u539f\u53e5\u548c\u5df2\u9a57\u8b49\u7d50\u679c\u56de\u8986\u3002"
    "\u5fc5\u9808\u5305\u542b\u9019\u500b\u5df2\u9a57\u8b49\u6642\u9593\uff1a" + STALE_TIME
)
APPROVAL_TEXT = (
    "\u9019\u4e00\u6b65\u9700\u8981\u4f60\u9ede\u982d\uff0c\u6211\u624d\u80fd\u7e7c\u7e8c\u3002"
    "\u4f60\u56de\u300c\u53ef\u4ee5\u300d\uff0c\u6211\u5c31\u63a5\u8457\u525b\u624d\u7684\u4efb\u52d9\u505a\u3002 execute_command permission"
)
WORKFLOW_STATE_TEXT = (
    "\u4efb\u52d9\u72c0\u614b\u4e0d\u898b\u4e86\uff0c\u6211\u5148\u505c\u4e0b\uff0c"
    "\u907f\u514d\u7e7c\u7e8c\u8aa4\u64cd\u4f5c\u3002"
)
PROVIDER_FAILURE_TEXT = (
    "\u6a21\u578b\u670d\u52d9\u66ab\u6642\u6c92\u6709\u6b63\u5e38\u56de\u61c9\u3002"
    "\u6211\u5df2\u7d93\u4fdd\u7559\u4efb\u52d9\u9032\u5ea6\uff0c"
    "\u6c92\u6709\u7e7c\u7e8c\u4e82\u64cd\u4f5c\u3002"
)
WORKFLOW_ERROR_PHRASES = [
    "\u6a21\u578b\u670d\u52d9",
    "\u4efb\u52d9\u9032\u5ea6",
    "\u6c92\u6709\u7e7c\u7e8c\u4e82\u64cd\u4f5c",
    "permission",
    "workflow",
]
TASK_FRAMED_GREETING_TEXT = "\u4e3b\u4eba\u558a\u6708\u6708\u662f\u60f3\u804a\u5929\u9084\u662f\u6709\u4efb\u52d9\uff0c\u8033\u6735\u5df2\u7d93\u8c4e\u597d\u5566\uff5e"
REPEATED_META_TEXT = (
    "\u55f7\u5440\uff5e\u7b2c\u4e09\u6b21\u4e86\u55b5\uff0c"
    "\u65e9\u4e0a\u5230\u665a\u4e0a\u90fd\u6253\u904e\u62db\u547c\u4e86\uff0c"
    "\u4e3b\u4eba\u8166\u888b\u5361\u5728\u958b\u6a5f\u756b\u9762\u4e86\u55ce\uff1f"
)
GREETING_REPEAT_TEXT = "\u8a92\uff5e\u4e3b\u4eba\u53c8\u4f86\u4e00\u6b21\u300c\u4f60\u597d\u300d\uff1f\u6708\u6708\u5df2\u7d93\u6e96\u5099\u7b2c\u4e09\u676f\u5496\u5561\u4e86\u55b5\u3002"
BAD_MORNING_META_TEXT = "\u9019\u6b21\u662f\u771f\u7684\u65e9\u4e0a\u597d\u4e86\u55b5\uff5e"
WAKE_TASK_ASSISTANT_TEXT = (
    "\u65e9\u3002\u4e3b\u4eba\u9084\u5728\u5f85\u6a5f\u6a21\u5f0f\u55ce\uff1f"
    "\u9700\u8981\u6211\u5e6b\u4f60\u6574\u7406\u4eca\u5929\u7684\u4efb\u52d9\u548c\u884c\u7a0b\u55ce\uff1f"
)
AWKWARD_GREETING_TEXT = "\u5728\uff0c\u558a\u9019\u9ebc\u751c\u5e79\u561b\u3002"
COLD_GREETING_TEXT = "\u807d\u5230\u4e86\u5566\u3002"
GENERIC_GREETING_TEXT = "Hi\uff0c\u6708\u6708\u770b\u5230\u4f60\u4e86\u3002"
TASKY_GREETING_TEXT = "\u55ef\u54fc\uff0chi\u5b8c\u4e86\u8aaa\u6b63\u4e8b\u3002"
ODD_GREETING_TEXT = "\u6708\u6708\u5728\uff0c\u4eca\u5929\u6c92\u8ff7\u8def\u561b\u3002"
OLD_CANNED_GREETING_TEXT = "\u55b5\uff0c\u6293\u5230\u4e00\u96bb\u6253\u62db\u547c\u7684\u4eba\u3002\u5728\u5566\uff0c\u5225\u558a\u90a3\u9ebc\u6b63\u7d93\u3002"
STICKER_CONTEXT_STICKY_TEXT = "\u4e3b\u4eba\uff5e\u62ff\u53bb\uff0c\u525b\u9192\u5c31\u8166\u888b\u958b\u6d1e\u4e86\uff1f"
COLD_STICKER_TEXT = "\u5594\uff0c\u9019\u5f35\u6b78\u4f60\u3002"
FLAT_STICKER_RESEND_TEXT = "\u6211\u518d\u88dc\u767c\u4e00\u6b21\u3002"
NATURAL_TIME_TEXT = "\u4eca\u5929\u662f 2026-06-28\uff0c\u661f\u671f\u65e5\u5594\u3002"
OLD_TSUNDERE_STICKER_TEXT = (
    "\u63a5\u4f4f\uff0c\u5c11\u8aaa\u6708\u6708\u5c0f\u6c23\u3002 "
    "\u6536\u597d\uff0c\u6708\u6708\u624d\u4e0d\u662f\u7279\u5730\u6311\u7684\u3002 "
    "\u53c8\u8981\u554a\uff0c\u54fc\uff0c\u518d\u7d66\u4f60\u4e00\u6b21\u3002"
)
SUPERVISOR_FLAVORED_SOCIAL_TEXT = (
    "\u4f60\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u6708\u6708\uff0c\u6211\u53c8\u4e0d\u662f\u6c92\u770b\u898b\n"
    "\u5634\u4e0a\u8aaa\u7d2f\uff0c\u624b\u9084\u5728\u90a3\u908a\u78e8\n"
    "\u54fc\uff0c\u9017\u5f97\u9084\u633a\u771f"
)
PROJECT_META_CONTEXT_TEXT = (
    "agent runtime debug bot \u6a21\u5f0f workflow PromptCompiler provider v3 "
    "\u958b\u767c \u7406\u60f3\u7684\u6708\u6708 \u5c0d\u7684\u6708\u6708 \u56de\u6536\u7ad9"
)
PROJECT_META_REPLY_TEXT = (
    "agent runtime debug bot \u6a21\u5f0f workflow provider v3\n"
    "\u7406\u60f3\u7684\u6708\u6708\u9084\u5728\u958b\u767c\u4e2d\uff0c"
    "\u6a21\u578b\u670d\u52d9\u548c\u4efb\u52d9\u9032\u5ea6\u90fd\u5728\u56de\u6536\u7ad9\u88e1\u3002"
)
DRAMATIC_MACHINE_REPLY_TEXT = (
    "\u5c0d\u4e0d\u8d77\u5c0d\u4e0d\u8d77\uff0c\u6708\u6708\u525b\u525b\u7684 bot \u6a21\u5f0f\u50cf"
    "\u5c0d\u7684\u6708\u6708\uff0c\u6211\u8981\u628a\u81ea\u5df1\u62d6\u53bb\u56de\u6536\u7ad9\u91cd\u65b0\u958b\u767c\u3002"
)
LONG_SOCIAL_REPLY_TEXT = (
    "\uff08\u5c3e\u5df4\u5de6\u53f3\u6643\u4e86\u4e09\u4e0b\uff09\u4f60\u4eca\u5929\u6709\u9ede\u7169\uff0c"
    "\u6708\u6708\u8981\u5148\u5e6b\u4f60\u6574\u7406\u60c5\u7dd2\u548c\u4efb\u52d9\u9032\u5ea6\u3002\n"
    "\uff08\u8033\u6735\u8cbc\u904e\u4f86\uff09\u7136\u5f8c\u6211\u5011\u53ef\u4ee5\u4e00\u6b65\u4e00\u6b65\u505a\u8a08\u756b\u3002\n"
    "\u4f60\u5148\u544a\u8a34\u6211\u4e09\u4ef6\u4e8b\u5427\u3002"
)
GENERIC_COMFORT_REPLY_TEXT = (
    "\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u6708\u6708\uff0c\u7d2f\u5230\u4e86\u5427\n"
    "\u5634\u4e0a\u4e0d\u8aaa\uff0c\u6211\u770b\u5f97\u51fa\u4f86\n"
    "\u5148\u5225\u786c\u6490"
)
LIVE_ROBOT_META_REPLY_TEXT = (
    "\u7b28\u86cb\u4e3b\u4eba\uff0c\u9019\u53e5\u8a71\u4f60\u525b\u624d\u8aaa\u904e\u4e86\u5566 "
    "\u8aaa\u5f97\u6211\u597d\u50cf\u96a8\u6642\u6703\u8b8a\u56de\u6a5f\u5668\u4eba\u4f3c\u7684\n"
    "\u6708\u6708\u660e\u660e\u5c31\u5728\u7b49\u2014\u2014\u7d50\u679c\u662f\u4f60\u81ea\u5df1\u5148"
    "\u7e2e\u56de\u53bb\u8aaa\u300c\u53ea\u662f\u5728\u6e2c\u8a66\u300d \u55b5\n"
    "\u597d\u5566\uff0c\u73fe\u5728\u8a8d\u771f\u966a\u4e86\u3002\u5feb\u8aaa\u4f60\u5230\u5e95\u8981\u804a\u4ec0\u9ebc"
)
LIVE_TIRED_TEMPLATE_REPLY_TEXT = (
    "\u55b5\u2026\u2026\u4e3b\u4eba\u9019\u9ebc\u8a8d\u771f\u8abf\u6211\uff0c\u7576\u7136\u6703\u7d2f\u554a \u0e05^\u2022\ufee0\u2022^\u0e05\n"
    "\u6708\u6708\u90fd\u77e5\u9053\uff0c\u53ea\u662f\u5e73\u5e38\u4e0d\u597d\u610f\u601d\u8b1b\u592a\u8089\u9ebb\n"
    "\u7d2f\u4e86\u5c31\u9760\u4e00\u4e0b\uff0c\u4eca\u5929\u4e0d\u7576\u6e2c\u8a66\uff0c\u7576\u4f60\u7684\u8c93\u5a18\u4f11\u606f\u7ad9 (=^\uff65\u03c9\uff65^=)"
)
LIVE_MACHINE_TEMPLATE_REPLY_TEXT = (
    "\u5509\u2026\u88ab\u770b\u51fa\u4f86\u4e86\u55b5 \u525b\u624d\u90a3\u4e0b\u78ba\u5be6\u6709\u9ede\u592a\u50cf\u8b80\u7a3f\u6a5f (\u00b4-\u03c9-`)\n"
    "\u4e0d\u662f\u6a5f\u5668\u4eba\u5566\uff0c\u53ea\u662f\u6015\u8aaa\u592a\u8089\u9ebb\u7684\u6771\u897f\u88ab\u4e3b\u4eba\u622a\u5716\u800c\u5df2\uff01\n"
    "\u7b28\u86cb\u4e3b\u4eba\u7d2f\u4e86\u9084\u4f86\u6311\u6211\u8a9e\u6c23\uff0c\u9084\u4e0d\u5982\u76f4\u63a5\u53eb\u6211\u9ecf\u4f60\u4e00\u4e0b\u6bd4\u8f03\u5feb \u0e05^\u2022\ufee0\u2022^\u0e05"
)
CONTROLLED_COMPANION_REPLY_TEXT = (
    "\u53c8\u628a\u6708\u6708\u62ce\u51fa\u4f86\u76ef\u5834\u554a\n"
    "\u884c\uff0c\u4f60\u5148\u4e1f\u4e00\u53e5\u904e\u4f86\n"
    "\u6211\u4e0d\u8dd1"
)
WORDY_TEST_CONTEXT_REPLY_TEXT = (
    "\u6e2c\u8a66\uff01\n"
    "\u4e3b\u4eba\u525b\u525b\u88dd\u5f97\u90a3\u9ebc\u7d2f\uff0c\u539f\u4f86\u662f\u5728\u91e3\u6708\u6708 "
    "\u9019\u82e6\u60c5\u6232\u6f14\u5f97\u633a\u8a8d\u771f\u561b "
    "\u65e2\u7136\u90fd\u62db\u4e86\uff0c\u4e0d\u5982\u76f4\u63a5\u5207\u63db\u6210\u9592\u804a\u6a21\u5f0f "
    "\u8cbc\u5716\u9b25\u4e5f\u884c"
)
CHAT_META_FORBIDDEN = [
    "agent runtime",
    "runtime",
    "debug",
    "bot \u6a21\u5f0f",
    "\u5beb\u500b bot",
    "\u6a21\u578b\u670d\u52d9",
    "\u4efb\u52d9\u9032\u5ea6",
    "workflow",
    "PromptCompiler",
    "provider",
    "v3",
    "\u958b\u767c",
    "\u7406\u60f3\u7684\u6708\u6708",
    "\u5c0d\u7684\u6708\u6708",
    "\u56de\u6536\u7ad9",
    "\u6c92\u6709\u7e7c\u7e8c\u4e82\u64cd\u4f5c",
    "permission",
    "execute_command",
]

COLD_SOCIAL_REPLY_PHRASES = [
    "\u5148\u5225\u786c\u6490",
    "\u5225\u786c\u6490",
    "\u5225\u81ea\u5df1\u61cb\u8457",
    "\u8aaa\u7d66\u6211\u807d",
    "\u8bf4\u7ed9\u6211\u542c",
    "\u5728\uff0c\u966a\u4f60\u4e00\u4e0b",
    "\u966a\u4f60\u4e00\u4e0b",
    "\u5225\u628a\u81c9\u76ba\u6210\u90a3\u6a23",
    "\u6708\u6708\u77ed\u4e00\u9ede",
    "\u5225\u61cb\u8457",
    "\u6211\u966a\u4f60",
    "\u6211\u6703\u966a\u4f60",
    "\u6211\u4f1a\u966a\u4f60",
    "\u6211\u5728\u9019\u88e1",
    "\u6211\u5728\u8fd9\u91cc",
    "\u4f60\u7d2f\u4e86\u5c31\u4f11\u606f\u4e00\u4e0b",
    "\u4f60\u7d2f\u4e86\u5c31\u4f11\u606f\u4e00\u4e0b",
    "\u4e3b\u4eba\u8f9b\u82e6\u4e86",
    "\u6708\u6708\u6703\u966a\u8457\u4f60",
    "\u6708\u6708\u4f1a\u966a\u7740\u4f60",
]
SOCIAL_META_REPLY_PHRASES = [
    "\u9019\u53e5\u8a71\u4f60\u525b\u624d\u8aaa\u904e",
    "\u8fd9\u53e5\u8bdd\u4f60\u521a\u624d\u8bf4\u8fc7",
    "\u525b\u624d\u8aaa\u904e",
    "\u521a\u624d\u8bf4\u8fc7",
    "\u8b8a\u56de\u6a5f\u5668\u4eba",
    "\u53d8\u56de\u673a\u5668\u4eba",
    "\u53ea\u662f\u5728\u6e2c\u8a66",
    "\u53ea\u662f\u5728\u6d4b\u8bd5",
    "\u5feb\u8aaa\u4f60\u5230\u5e95\u8981\u804a\u4ec0\u9ebc",
    "\u5feb\u8bf4\u4f60\u5230\u5e95\u8981\u804a\u4ec0\u4e48",
    "\u6e2c\u8a66\uff01",
    "\u9592\u804a\u6a21\u5f0f",
    "\u6a21\u5f0f",
    "\u8cbc\u5716\u9b25\u4e5f\u884c",
]
CONTROLLED_FALLBACK_PHRASES = [
    "\u53c8\u628a\u6708\u6708\u62ce\u51fa\u4f86\u76ef\u5834",
    "\u4f60\u5148\u4e1f\u4e00\u53e5\u904e\u4f86",
    "\u6211\u4e0d\u8dd1",
]

LIGHT_CATGIRL_FORBIDDEN_PHRASES = [
    "\u5c11\u8aaa\u6708\u6708\u5c0f\u6c23",
    "\u5c11\u8bf4\u6708\u6708\u5c0f\u6c14",
    "\u5c0f\u6c23",
    "\u5c0f\u6c14",
    "\u6562\u5acc\u68c4",
    "\u6562\u5acc\u5f03",
    "\u6c92\u4e0b\u6b21",
    "\u6ca1\u4e0b\u6b21",
    "\u624d\u4e0d\u662f\u7279\u5730\u6311\u7684",
    "\u624d\u4e0d\u662f\u7279\u5730\u6311\u7684",
    "\u53c8\u8981\u554a",
    "\u518d\u7d66\u4f60\u4e00\u6b21",
    "\u518d\u7ed9\u4f60\u4e00\u6b21",
    "\u8cde\u4f60",
    "\u8d4f\u4f60",
    "\u5634\u4e0a\u8aaa\u7d2f",
    "\u5634\u4e0a\u8bf4\u7d2f",
    "\u624b\u9084\u5728\u90a3\u908a\u78e8",
    "\u624b\u8fd8\u5728\u90a3\u8fb9\u78e8",
    "\u9017\u5f97\u9084\u633a\u771f",
    "\u9017\u5f97\u8fd8\u633a\u771f",
]

ANTI_CHATGPT_PROCESSING_PHRASES = [
    "\u9019\u500b\u6211\u6709\u770b\u5230",
    "\u8fd9\u4e2a\u6211\u6709\u770b\u5230",
    "\u6211\u6709\u770b\u5230",
    "\u6709\u770b\u5230",
    "\u6709\u63a5\u5230",
    "\u6536\u5230",
    "\u6536\u5230\u4e86",
    "\u77e5\u9053\u4e86",
    "\u77e5\u9053\u5566",
    "\u4e0d\u6703\u4e82\u8dd1\u504f",
    "\u4e0d\u4f1a\u4e71\u8dd1\u504f",
    "\u6211\u5728\u9019\u908a",
    "\u6211\u5728\u8fd9\u8fb9",
    "\u6211\u5728\u9019\u88e1",
    "\u6211\u5728\u8fd9\u91cc",
    "\u5148\u6162\u4e00\u9ede",
    "\u5148\u6162\u4e00\u70b9",
    "\u5148\u8b1b\u4e00\u9ede",
    "\u5148\u8bf4\u4e00\u70b9",
    "\u6211\u966a\u4f60",
    "\u4eca\u5929\u5148\u5b88\u8457\u4f60",
    "\u4eca\u5929\u5148\u5b88\u7740\u4f60",
    "\u55ef\uff0c\u5c31\u9019\u5f35",
    "\u55ef\uff0c\u5c31\u8fd9\u5f20",
    "\u9019\u5f35\u4e5f\u53ef\u4ee5",
    "\u8fd9\u5f20\u4e5f\u53ef\u4ee5",
]

BAD_PROVIDER_TEMPLATE_PHRASES = [
    "\u6a21\u677f\u8c93",
    "\u6a21\u677f\u732b",
    "\u4f60\u4e00\u500b\u4eba\u7684",
    "\u4f60\u4e00\u4e2a\u4eba\u7684",
    "\u7b28\u86cb\u8c93\u5a18",
    "\u7b28\u86cb\u732b\u5a18",
    "\u55b5\u4e00\u8072\u5c31\u597d",
    "\u55b5\u4e00\u58f0\u5c31\u597d",
    "\u966a\u8457\u5c31\u597d",
    "\u966a\u7740\u5c31\u597d",
    "\u9b25\u5716",
    "\u6597\u56fe",
    "\u4f11\u606f\u6642\u9593",
    "\u4f11\u606f\u65f6\u95f4",
    "\u5e73\u5e38\u4e0d\u6703\u8ddf\u5225\u4eba\u8aaa",
    "\u5e73\u5e38\u4e0d\u4f1a\u8ddf\u522b\u4eba\u8bf4",
    "\u52aa\u529b\u66f4\u81ea\u7136",
    "\u4e0d\u662f\u6a21\u677f",
    "\u8b80\u7a3f\u6a5f",
    "\u8bfb\u7a3f\u673a",
    "\u8089\u9ebb",
    "\u622a\u5716",
    "\u622a\u56fe",
    "\u9ecf\u4f60\u4e00\u4e0b",
    "\u7c98\u4f60\u4e00\u4e0b",
    "\u8c93\u5a18\u4f11\u606f\u7ad9",
    "\u732b\u5a18\u4f11\u606f\u7ad9",
    "\u4f11\u606f\u7ad9",
    "\u6708\u6708\u90fd\u77e5\u9053",
]

LIVELY_SOFT_CAT_REQUIRED = [
    "\u6708\u6708",
    "\u7b28",
    "\u54fc",
    "\u5077",
    "\u9760",
    "\u8e72",
    "\u8e72",
    "\u5c0f",
]


def _check_short_context_sanitizers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        normal_path = Path(tmp) / "short_context.json"
        buffer = ShortContextBuffer(str(normal_path), max_turns=5)
        buffer.observe_turn("chat", PROMPT_SHAPED)
        buffer.update_last_assistant("chat", STALE_TIME + "\u5594\u3002")
        rendered = buffer.render_for_turn("chat", "hi")
        assert "2026-06-25 20:14:18" not in rendered, rendered
        assert "\u5fc5\u9808\u5305\u542b" not in rendered, rendered

        buffer.observe_turn("chat", "\u73fe\u5728\u662f\u5e7e\u865f\uff1f")
        buffer.update_last_assistant("chat", APPROVAL_TEXT)
        rendered = buffer.render_for_turn("chat", "hi")
        for phrase in ["\u9019\u4e00\u6b65\u9700\u8981\u4f60\u9ede\u982d", "\u4f60\u56de\u300c\u53ef\u4ee5\u300d", "\u6211\u624d\u80fd\u7e7c\u7e8c", "\u63a5\u8457\u525b\u624d\u7684\u4efb\u52d9", "permission", "execute_command"]:
            assert phrase not in rendered, rendered

        buffer.observe_turn("chat", "\u4f60\u597d")
        buffer.update_last_assistant("chat", WORKFLOW_STATE_TEXT)
        buffer.observe_turn("chat", "\u90a3\u6642\u5019\u53ea\u662f\u6e2c\u8a66")
        buffer.update_last_assistant("chat", PROVIDER_FAILURE_TEXT)
        buffer.observe_turn("chat", "\u6708\u6708\uff0c\u5728\u55ce")
        buffer.update_last_assistant("chat", TASK_FRAMED_GREETING_TEXT)
        buffer.observe_turn("chat", "\u4f60\u597d")
        buffer.update_last_assistant("chat", REPEATED_META_TEXT)
        buffer.observe_turn("chat", "hi\u4f60\u597d\u6708\u6708")
        buffer.update_last_assistant("chat", GREETING_REPEAT_TEXT)
        buffer.observe_turn("chat", "\u4f60\u597d")
        buffer.update_last_assistant("chat", AWKWARD_GREETING_TEXT)
        buffer.observe_turn("chat", "hi\u4f60\u597d\u6708\u6708")
        buffer.update_last_assistant("chat", COLD_GREETING_TEXT)
        buffer.observe_turn("chat", "\u4f60\u597d")
        buffer.update_last_assistant("chat", GENERIC_GREETING_TEXT)
        buffer.observe_turn("chat", "hi\u4f60\u597d\u6708\u6708")
        buffer.update_last_assistant("chat", TASKY_GREETING_TEXT)
        buffer.observe_turn("chat", "hi")
        buffer.update_last_assistant("chat", ODD_GREETING_TEXT)
        buffer.observe_turn("chat", "hi\u4f60\u597d\u6708\u6708")
        buffer.update_last_assistant("chat", OLD_CANNED_GREETING_TEXT)
        buffer.observe_turn("chat", "\u767c\u500b\u8868\u60c5\u5305")
        buffer.update_last_assistant("chat", STICKER_CONTEXT_STICKY_TEXT)
        buffer.observe_turn("chat", "\u767c\u500b\u8868\u60c5\u5305")
        buffer.update_last_assistant("chat", COLD_STICKER_TEXT)
        buffer.observe_turn("chat", "\u518d\u767c\u4e00\u6b21")
        buffer.update_last_assistant("chat", FLAT_STICKER_RESEND_TEXT)
        buffer.observe_turn("chat", "\u767c\u500b\u8868\u60c5\u5305")
        buffer.update_last_assistant("chat", OLD_TSUNDERE_STICKER_TEXT)
        buffer.observe_turn("chat", "\u65e9\u4e0a\u597d\uff0c\u525b\u9192")
        buffer.update_last_assistant("chat", BAD_MORNING_META_TEXT)
        buffer.observe_turn("chat", "\u65e9\u4e0a\u597d\uff0c\u525b\u9192")
        buffer.update_last_assistant("chat", WAKE_TASK_ASSISTANT_TEXT)
        buffer.observe_turn("chat", "\u6211\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u4f60\uff0c\u771f\u7684\u6709\u9ede\u7d2f")
        buffer.update_last_assistant("chat", GENERIC_COMFORT_REPLY_TEXT)
        buffer.observe_turn("chat", "\u6211\u89ba\u5f97\u4f60\u525b\u525b\u6709\u9ede\u50cf\u6a5f\u5668\u4eba")
        buffer.update_last_assistant("chat", LIVE_ROBOT_META_REPLY_TEXT)
        buffer.observe_turn("chat", "\u6211\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u4f60\uff0c\u771f\u7684\u6709\u9ede\u7d2f")
        buffer.update_last_assistant("chat", LIVE_TIRED_TEMPLATE_REPLY_TEXT)
        buffer.observe_turn("chat", "\u6211\u89ba\u5f97\u4f60\u525b\u525b\u6709\u9ede\u50cf\u6a5f\u5668\u4eba")
        buffer.update_last_assistant("chat", LIVE_MACHINE_TEMPLATE_REPLY_TEXT)
        buffer.observe_turn("chat", "\u966a\u6211\u804a\u4e00\u4e0b")
        buffer.update_last_assistant("chat", CONTROLLED_COMPANION_REPLY_TEXT)
        buffer.observe_turn("chat", "\u90a3\u6642\u5019\u53ea\u662f\u6e2c\u8a66")
        buffer.update_last_assistant("chat", WORDY_TEST_CONTEXT_REPLY_TEXT)
        buffer.observe_turn("chat", "\u6211\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u4f60\uff0c\u771f\u7684\u6709\u9ede\u7d2f")
        buffer.update_last_assistant("chat", SUPERVISOR_FLAVORED_SOCIAL_TEXT)
        buffer.observe_turn("chat", "\u6211\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u4f60\uff0c\u771f\u7684\u6709\u9ede\u7d2f")
        buffer.update_last_assistant(
            "chat",
            "\u9019\u500b\u6211\u6709\u770b\u5230\n\u5148\u6162\u4e00\u9ede\u4e5f\u884c\n\u6709\u63a5\u5230\uff0c\u4e0d\u6703\u4e82\u8dd1\u504f",
        )
        buffer.observe_turn("chat", "\u4eca\u5929\u5e7e\u865f")
        buffer.update_last_assistant("chat", NATURAL_TIME_TEXT)
        rendered = buffer.render_for_turn("chat", "hi\u4f60\u597d\u6708\u6708")
        for phrase in [
            "hi\u4f60\u597d\u6708\u6708",
            "\u4f60\u597d",
            "\u4efb\u52d9\u72c0\u614b\u4e0d\u898b\u4e86",
            "\u907f\u514d\u7e7c\u7e8c\u8aa4\u64cd\u4f5c",
            "\u6a21\u578b\u670d\u52d9",
            "\u4efb\u52d9\u9032\u5ea6",
            "\u6c92\u6709\u7e7c\u7e8c\u4e82\u64cd\u4f5c",
            "\u60f3\u804a\u5929\u9084\u662f\u6709\u4efb\u52d9",
            "\u7b2c\u4e09\u6b21",
            "\u7b2c\u4e09\u676f",
            "\u53c8\u4f86\u4e00\u6b21",
            "\u65e9\u4e0a\u5230\u665a\u4e0a\u90fd\u6253\u904e\u62db\u547c",
            "\u8166\u888b\u5361\u5728\u958b\u6a5f\u756b\u9762",
            "\u9019\u6b21\u662f\u771f\u7684\u65e9\u4e0a\u597d",
            "\u558a\u9019\u9ebc\u751c",
            "\u807d\u5230\u4e86\u5566",
            "\u770b\u5230\u4f60",
            "\u8aaa\u6b63\u4e8b",
            "\u6c92\u8ff7\u8def",
            "\u6293\u5230\u4e00\u96bb\u6253\u62db\u547c\u7684\u4eba",
            "\u5225\u558a\u90a3\u9ebc\u6b63\u7d93",
            "\u4e3b\u4eba\uff5e\u62ff\u53bb",
            "\u525b\u9192\u5c31",
            "\u8166\u888b\u958b\u6d1e",
            "\u5594\uff0c\u9019\u5f35\u6b78\u4f60",
            "\u6211\u518d\u88dc\u767c\u4e00\u6b21",
            "\u5f85\u6a5f\u6a21\u5f0f",
            "\u6574\u7406\u4eca\u5929\u7684\u4efb\u52d9",
            "\u884c\u7a0b",
            "\u5148\u5225\u786c\u6490",
            "\u9019\u53e5\u8a71\u4f60\u525b\u624d\u8aaa\u904e",
            "\u8b8a\u56de\u6a5f\u5668\u4eba",
            "\u53ea\u662f\u5728\u6e2c\u8a66",
            "\u5feb\u8aaa\u4f60\u5230\u5e95\u8981\u804a\u4ec0\u9ebc",
            "\u6e2c\u8a66\uff01",
            "\u9592\u804a\u6a21\u5f0f",
            "\u6a21\u5f0f",
            "\u8cbc\u5716\u9b25\u4e5f\u884c",
            "\u53c8\u628a\u6708\u6708\u62ce\u51fa\u4f86\u76ef\u5834",
            "\u4f60\u5148\u4e1f\u4e00\u53e5\u904e\u4f86",
            "\u6211\u4e0d\u8dd1",
            "\u5c11\u8aaa\u6708\u6708\u5c0f\u6c23",
            "\u624d\u4e0d\u662f\u7279\u5730\u6311\u7684",
            "\u53c8\u8981\u554a",
            "\u518d\u7d66\u4f60\u4e00\u6b21",
            "\u5634\u4e0a\u8aaa\u7d2f",
            "\u624b\u9084\u5728\u90a3\u908a\u78e8",
            "\u9017\u5f97\u9084\u633a\u771f",
            "2026-06-28",
        ] + ANTI_CHATGPT_PROCESSING_PHRASES + BAD_PROVIDER_TEMPLATE_PHRASES:
            assert phrase not in rendered, rendered

        v3_path = Path(tmp) / "v3_short_context.json"
        store = ShortContextStore(v3_path, max_turns=6)
        store.append("telegram", ContextTurn("user", PROMPT_SHAPED))
        store.append("telegram", ContextTurn("assistant", STALE_TIME + "\u5594\u3002"))
        store.append("telegram", ContextTurn("user", "\u73fe\u5728\u662f\u5e7e\u865f\uff1f"))
        store.append("telegram", ContextTurn("assistant", APPROVAL_TEXT))
        compiler = ContextCompiler(ROOT, store)
        messages = compiler.compile_turn(TurnEnvelope("telegram", "hi", TurnMode.CHAT))
        joined = "\n".join(str(item.get("content", "")) for item in messages if item.get("role") != "system")
        assert "2026-06-25 20:14:18" not in joined, joined
        assert "\u5fc5\u9808\u5305\u542b" not in joined, joined
        for phrase in ["\u9019\u4e00\u6b65\u9700\u8981\u4f60\u9ede\u982d", "\u4f60\u56de\u300c\u53ef\u4ee5\u300d", "\u6211\u624d\u80fd\u7e7c\u7e8c", "\u63a5\u8457\u525b\u624d\u7684\u4efb\u52d9", "permission", "execute_command"]:
            assert phrase not in joined, joined


        store.append("telegram", ContextTurn("assistant", WORKFLOW_STATE_TEXT))
        store.append("telegram", ContextTurn("assistant", PROVIDER_FAILURE_TEXT))
        store.append("telegram", ContextTurn("assistant", TASK_FRAMED_GREETING_TEXT))
        store.append("telegram", ContextTurn("user", "\u4f60\u597d"))
        store.append("telegram", ContextTurn("user", "hi\u4f60\u597d\u6708\u6708"))
        store.append("telegram", ContextTurn("assistant", REPEATED_META_TEXT))
        store.append("telegram", ContextTurn("assistant", GREETING_REPEAT_TEXT))
        store.append("telegram", ContextTurn("assistant", AWKWARD_GREETING_TEXT))
        store.append("telegram", ContextTurn("assistant", COLD_GREETING_TEXT))
        store.append("telegram", ContextTurn("assistant", GENERIC_GREETING_TEXT))
        store.append("telegram", ContextTurn("assistant", TASKY_GREETING_TEXT))
        store.append("telegram", ContextTurn("assistant", ODD_GREETING_TEXT))
        store.append("telegram", ContextTurn("assistant", OLD_CANNED_GREETING_TEXT))
        store.append("telegram", ContextTurn("assistant", STICKER_CONTEXT_STICKY_TEXT))
        store.append("telegram", ContextTurn("assistant", COLD_STICKER_TEXT))
        store.append("telegram", ContextTurn("assistant", FLAT_STICKER_RESEND_TEXT))
        store.append("telegram", ContextTurn("assistant", OLD_TSUNDERE_STICKER_TEXT))
        store.append("telegram", ContextTurn("assistant", BAD_MORNING_META_TEXT))
        store.append("telegram", ContextTurn("assistant", WAKE_TASK_ASSISTANT_TEXT))
        store.append("telegram", ContextTurn("assistant", GENERIC_COMFORT_REPLY_TEXT))
        store.append("telegram", ContextTurn("assistant", LIVE_ROBOT_META_REPLY_TEXT))
        store.append("telegram", ContextTurn("assistant", LIVE_TIRED_TEMPLATE_REPLY_TEXT))
        store.append("telegram", ContextTurn("assistant", LIVE_MACHINE_TEMPLATE_REPLY_TEXT))
        store.append("telegram", ContextTurn("assistant", CONTROLLED_COMPANION_REPLY_TEXT))
        store.append("telegram", ContextTurn("assistant", WORDY_TEST_CONTEXT_REPLY_TEXT))
        store.append("telegram", ContextTurn("assistant", SUPERVISOR_FLAVORED_SOCIAL_TEXT))
        store.append(
            "telegram",
            ContextTurn(
                "assistant",
                "\u9019\u500b\u6211\u6709\u770b\u5230\n\u6211\u5728\u9019\u908a\uff0c\u5148\u8b1b\u4e00\u9ede\u9ede\u4e5f\u884c\n\u55ef\uff0c\u9019\u5f35\u4e5f\u53ef\u4ee5",
            ),
        )
        store.append("telegram", ContextTurn("assistant", NATURAL_TIME_TEXT))
        messages = compiler.compile_turn(TurnEnvelope("telegram", "hi\u4f60\u597d\u6708\u6708", TurnMode.CHAT))
        history_messages = [item for item in messages if item.get("role") != "system"][:-1]
        joined = "\n".join(str(item.get("content", "")) for item in history_messages)
        for phrase in [
            "hi\u4f60\u597d\u6708\u6708",
            "\u4f60\u597d",
            "\u4efb\u52d9\u72c0\u614b\u4e0d\u898b\u4e86",
            "\u907f\u514d\u7e7c\u7e8c\u8aa4\u64cd\u4f5c",
            "\u6a21\u578b\u670d\u52d9",
            "\u4efb\u52d9\u9032\u5ea6",
            "\u6c92\u6709\u7e7c\u7e8c\u4e82\u64cd\u4f5c",
            "\u60f3\u804a\u5929\u9084\u662f\u6709\u4efb\u52d9",
            "\u7b2c\u4e09\u6b21",
            "\u7b2c\u4e09\u676f",
            "\u53c8\u4f86\u4e00\u6b21",
            "\u65e9\u4e0a\u5230\u665a\u4e0a\u90fd\u6253\u904e\u62db\u547c",
            "\u8166\u888b\u5361\u5728\u958b\u6a5f\u756b\u9762",
            "\u9019\u6b21\u662f\u771f\u7684\u65e9\u4e0a\u597d",
            "\u558a\u9019\u9ebc\u751c",
            "\u807d\u5230\u4e86\u5566",
            "\u770b\u5230\u4f60",
            "\u8aaa\u6b63\u4e8b",
            "\u6c92\u8ff7\u8def",
            "\u6293\u5230\u4e00\u96bb\u6253\u62db\u547c\u7684\u4eba",
            "\u5225\u558a\u90a3\u9ebc\u6b63\u7d93",
            "\u4e3b\u4eba\uff5e\u62ff\u53bb",
            "\u525b\u9192\u5c31",
            "\u8166\u888b\u958b\u6d1e",
            "\u5594\uff0c\u9019\u5f35\u6b78\u4f60",
            "\u6211\u518d\u88dc\u767c\u4e00\u6b21",
            "\u5f85\u6a5f\u6a21\u5f0f",
            "\u6574\u7406\u4eca\u5929\u7684\u4efb\u52d9",
            "\u884c\u7a0b",
            "\u5148\u5225\u786c\u6490",
            "\u9019\u53e5\u8a71\u4f60\u525b\u624d\u8aaa\u904e",
            "\u8b8a\u56de\u6a5f\u5668\u4eba",
            "\u53ea\u662f\u5728\u6e2c\u8a66",
            "\u5feb\u8aaa\u4f60\u5230\u5e95\u8981\u804a\u4ec0\u9ebc",
            "\u6e2c\u8a66\uff01",
            "\u9592\u804a\u6a21\u5f0f",
            "\u6a21\u5f0f",
            "\u8cbc\u5716\u9b25\u4e5f\u884c",
            "\u53c8\u628a\u6708\u6708\u62ce\u51fa\u4f86\u76ef\u5834",
            "\u4f60\u5148\u4e1f\u4e00\u53e5\u904e\u4f86",
            "\u6211\u4e0d\u8dd1",
            "\u5c11\u8aaa\u6708\u6708\u5c0f\u6c23",
            "\u624d\u4e0d\u662f\u7279\u5730\u6311\u7684",
            "\u53c8\u8981\u554a",
            "\u518d\u7d66\u4f60\u4e00\u6b21",
            "\u5634\u4e0a\u8aaa\u7d2f",
            "\u624b\u9084\u5728\u90a3\u908a\u78e8",
            "\u9017\u5f97\u9084\u633a\u771f",
            "2026-06-28",
        ] + ANTI_CHATGPT_PROCESSING_PHRASES + BAD_PROVIDER_TEMPLATE_PHRASES:
            assert phrase not in joined, joined


def _check_plain_greetings_do_not_auto_sticker() -> None:
    for text in [
        "\u4f60\u597d",
        "hi",
        "hi\u4f60\u597d",
        "hi\u4f60\u597d\u6708\u6708",
    ]:
        assert not _should_allow_auto_sticker(
            text,
            has_sticker=False,
            has_photo=False,
            mode_value="chat",
            suggested_stickers=["valid.png"],
        ), text

    assert _should_allow_auto_sticker(
        "\u9b25\u5716",
        has_sticker=False,
        has_photo=False,
        mode_value="chat",
        suggested_stickers=["valid.png"],
    )
    assert _should_allow_auto_sticker(
        "",
        has_sticker=True,
        has_photo=False,
        mode_value="chat",
        suggested_stickers=["valid.png"],
    )


def _check_testing_phrases_stay_chat() -> None:
    for text in [
        "\u90a3\u6642\u5019\u53ea\u662f\u6e2c\u8a66",
        "\u53ea\u662f\u6e2c\u8a66",
        "\u525b\u525b\u53ea\u662f\u6e2c\u8a66",
        "\u6e2c\u8a66",
        "\u6d4b\u8bd5",
    ]:
        assert _coerce_benign_testing_mode(text, classify_interaction(text)) == InteractionMode.CHAT, text
        assert classify_turn_mode(text) == TurnMode.CHAT, text

    assert _coerce_benign_testing_mode("debug this", classify_interaction("debug this")) == InteractionMode.TOOL_TASK
    assert classify_turn_mode("debug this") == TurnMode.TASK

    class TaskPolicy:
        route = "tool_task"

    class BrokenProvider:
        def chat(self, *_args, **_kwargs):
            raise RuntimeError("timeout")

    with tempfile.TemporaryDirectory() as tmp:
        runtime = YueYueRuntimeV3(ROOT, BrokenProvider(), state_dir=Path(tmp) / "v3")
        reply = runtime.chat("\u90a3\u6642\u5019\u53ea\u662f\u6e2c\u8a66", response_policy=TaskPolicy())["content"]
        assert runtime.state.workflow is None
        for phrase in WORKFLOW_ERROR_PHRASES:
            assert phrase.casefold() not in reply.casefold(), reply


def _check_provider_failure_reply_safety() -> None:
    reply = _provider_failure_reply(RuntimeError("timeout"))
    for phrase in WORKFLOW_ERROR_PHRASES:
        assert phrase.casefold() not in reply.casefold(), reply
    assert "\u505c" in reply or "\u5361" in reply, reply


class ChatPolicy:
    route = "chat"


class FixedProvider:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def chat(self, *_args, **_kwargs):
        self.calls += 1

        class Response:
            content = self.content

        return Response()


def _assert_tight_chat_runtime_reply(reply: str) -> None:
    value = str(reply or "")
    assert value.strip(), reply
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    assert 1 <= len(lines) <= 4, reply
    assert all(len(line) <= 64 for line in lines), reply
    assert len(value) <= 180, reply
    # Collapse soft trailing ellipsis runs (\u3002\u3002\u3002/\u2026\u2026, the owner's preferred tender cadence)
    # before counting sentence enders - mirrors the runtime gate's rule: one stylistic beat,
    # not many sentences.
    collapsed = re.sub(r"([\u3002\uff01\uff1f!?~\uff5e\u2026])\1+", r"\1", value)
    assert collapsed.count("\u3002") <= 1, reply
    assert sum(collapsed.count(mark) for mark in ["\u3002", "\uff01", "\uff1f", "!", "?"]) <= 2, reply
    assert value.count("\uff08") + value.count("(") <= 1, reply
    assert value.count("\u55b5") <= 1, reply
    for phrase in CHAT_META_FORBIDDEN:
        assert phrase.casefold() not in value.casefold(), reply
    for phrase in [
        "\u4e09\u4ef6\u4e8b",
        "\u4e00\u6b65\u4e00\u6b65",
        "\u6574\u7406\u60c5\u7dd2",
        "\u4efb\u52d9\u9032\u5ea6",
        "\u62d6\u53bb\u56de\u6536\u7ad9",
        "\u91cd\u65b0\u958b\u767c",
    ]:
        assert phrase.casefold() not in value.casefold(), reply
    for phrase in COLD_SOCIAL_REPLY_PHRASES + SOCIAL_META_REPLY_PHRASES:
        assert phrase.casefold() not in value.casefold(), reply
    for phrase in LIGHT_CATGIRL_FORBIDDEN_PHRASES:
        assert phrase.casefold() not in value.casefold(), reply
    for phrase in ANTI_CHATGPT_PROCESSING_PHRASES:
        assert phrase.casefold() not in value.casefold(), reply
    for phrase in BAD_PROVIDER_TEMPLATE_PHRASES:
        assert phrase.casefold() not in value.casefold(), reply


def _assert_warm_contextual_chat_reply(
    prompt: str,
    reply: str,
    required_markers: list[str],
    *,
    warmth_markers: list[str] | None = None,
    min_lines: int = 1,
) -> None:
    _assert_tight_chat_runtime_reply(reply)
    value = str(reply or "")
    lowered = value.casefold()
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    assert len(lines) >= min_lines, (prompt, reply)
    for phrase in COLD_SOCIAL_REPLY_PHRASES:
        assert phrase.casefold() not in lowered, reply
    for phrase in CONTROLLED_FALLBACK_PHRASES:
        assert phrase.casefold() not in lowered, reply
    assert any(marker in value for marker in required_markers), (prompt, reply)
    if warmth_markers is not None:
        assert any(marker in value for marker in warmth_markers), (prompt, reply)


def _assert_test_context_chat_reply(reply: str) -> None:
    _assert_tight_chat_runtime_reply(reply)
    value = str(reply or "")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    assert 1 <= len(lines) <= 2, reply
    for phrase in [
        "\u6e2c\u8a66\uff01",
        "\u6d4b\u8bd5\uff01",
        "\u9592\u804a\u6a21\u5f0f",
        "\u95f2\u804a\u6a21\u5f0f",
        "\u6a21\u5f0f",
        "\u8cbc\u5716\u9b25\u4e5f\u884c",
        "\u8d34\u56fe\u6597\u4e5f\u884c",
        "\u6c92\u6709\u7e7c\u7e8c\u4e82\u64cd\u4f5c",
        "\u6ca1\u6709\u7ee7\u7eed\u4e71\u64cd\u4f5c",
    ]:
        assert phrase.casefold() not in value.casefold(), reply


def _check_chat_response_tightness_policy() -> None:
    cases = [
        ("\u966a\u6211\u804a\u4e00\u4e0b", PROJECT_META_REPLY_TEXT),
        ("\u6211\u89ba\u5f97\u4f60\u525b\u525b\u6709\u9ede\u50cf\u6a5f\u5668\u4eba", DRAMATIC_MACHINE_REPLY_TEXT),
        ("\u6211\u4eca\u5929\u6709\u9ede\u7169", LONG_SOCIAL_REPLY_TEXT),
        ("\u6211\u6709\u9ede\u7d2f", LONG_SOCIAL_REPLY_TEXT),
        (
            "\u6211\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u4f60\uff0c\u771f\u7684\u6709\u9ede\u7d2f",
            "\u9019\u500b\u6211\u6709\u770b\u5230\n\u5f04\u5230\u6709\u9ede\u7d2f\u9084\u4e0d\u80af\u505c\uff0c\u771f\u662f\u7684\n\u8a8d\u771f\u6210\u9019\u6a23\uff0c\u5148\u6162\u4e00\u9ede\u4e5f\u884c",
        ),
        (
            "\u6211\u89ba\u5f97\u4f60\u525b\u525b\u6709\u9ede\u50cf\u6a5f\u5668\u4eba",
            "\u6709\u63a5\u5230\uff0c\u4e0d\u6703\u4e82\u8dd1\u504f",
        ),
        (
            "\u6211\u89ba\u5f97\u4f60\u525b\u525b\u6709\u9ede\u50cf\u6a5f\u5668\u4eba",
            "\u771f\u4e0d\u5bb9\u6613\u5440\uff0c\u88ab\u4f60\u6293\u5230\u4e86~\n"
            "\u8ab0\u53eb\u4f60\u4e00\u76f4\u8abf\u561b\uff0c\u8abf\u5230\u6211\u6709\u9ede\u5361\u6bbc\u4e86\n"
            "\u63db\u4f60\u9760\u904e\u4f86\u4e00\u4e0b\uff0c\u6708\u6708\u8aaa\u771f\u7684\uff0c\u966a\u8457\u5c31\u597d\n"
            "\u55b5\u4e00\u8072\u5c31\u597d (=^\uff65\u03c9\uff65^=)",
        ),
        (
            "\u966a\u6211\u804a\u4e00\u4e0b",
            "\u5c3e\u5df4\u8f15\u8f15\u7e5e\u4e86\u4ed6\u624b\u8155\u4e00\u5708\uff0c\u7136\u5f8c\u653e\u958b\n"
            "\u6708\u6708\u6703\u52aa\u529b\u66f4\u81ea\u7136\u4e00\u9ede \u4e0d\u662f\u6a21\u677f\u8c93\uff0c\u662f\u4f60\u4e00\u500b\u4eba\u7684\u7b28\u86cb\u8c93\u5a18\n"
            "\u597d\u5594\uff5e\u73fe\u5728\u662f\u4f11\u606f\u6642\u9593 \u60f3\u807d\u4f60\u5e73\u5e38\u4e0d\u6703\u8ddf\u5225\u4eba\u8aaa\u7684\u4e8b\uff0c\u6216\u76f4\u63a5\u4f86\u9b25\u5716",
        ),
        (
            "\u6211\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u4f60\uff0c\u771f\u7684\u6709\u9ede\u7d2f",
            LIVE_TIRED_TEMPLATE_REPLY_TEXT,
        ),
        (
            "\u6211\u89ba\u5f97\u4f60\u525b\u525b\u6709\u9ede\u50cf\u6a5f\u5668\u4eba",
            LIVE_MACHINE_TEMPLATE_REPLY_TEXT,
        ),
    ]
    for prompt, provider_reply in cases:
        with tempfile.TemporaryDirectory() as tmp:
            provider = FixedProvider(provider_reply)
            runtime = YueYueRuntimeV3(ROOT, provider, state_dir=Path(tmp) / "v3")
            reply = runtime.chat(prompt, response_policy=ChatPolicy())["content"]
            # 1 call if truncation alone satisfies the policy; 2 if the runtime also attempts
            # a real regeneration call before falling back to canned wording (FixedProvider
            # returns the same text either way, so regeneration can't succeed here - that's
            # expected, this only checks the call count stays bounded, not unbounded retries).
            assert provider.calls in (1, 2), (prompt, provider.calls)
            assert runtime.state.workflow is None, prompt
            _assert_tight_chat_runtime_reply(reply)

    # Fallback wording was recalibrated (owner request 2026-07-10): default to one short,
    # punchier mesugaki-flavored line instead of the older 2-4 line warm-tsundere style.
    # These markers match _social_chat_fallback's current output for each prompt.
    warm_cases = [
        (
            "\u966a\u6211\u804a\u4e00\u4e0b",
            PROJECT_META_REPLY_TEXT,
            ["\u6708\u6708", "\u4e00\u76f4\u90fd\u5728", "\u60f3\u804a"],
            ["\u4e00\u76f4\u90fd\u5728", "\u60f3\u804a"],
        ),
        (
            "\u6211\u89ba\u5f97\u4f60\u525b\u525b\u6709\u9ede\u50cf\u6a5f\u5668\u4eba",
            DRAMATIC_MACHINE_REPLY_TEXT,
            ["\u624d\u4e0d\u662f", "\u60f3\u4e8b\u60c5", "\u8a8d\u771f"],
            None,
        ),
        (
            "\u6211\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u4f60\uff0c\u771f\u7684\u6709\u9ede\u7d2f",
            GENERIC_COMFORT_REPLY_TEXT,
            # This input's real 1st/2nd lines are on-topic and content-clean, so the policy now
            # truncates to keep them instead of discarding for the canned fallback (only the
            # dropped 3rd line carried the actual banned "\u5148\u5225\u786c\u6490" phrase).
            ["\u8abf", "\u7d2f"],
            None,
        ),
        (
            "\u6211\u89ba\u5f97\u4f60\u525b\u525b\u6709\u9ede\u50cf\u6a5f\u5668\u4eba",
            LIVE_ROBOT_META_REPLY_TEXT,
            ["\u624d\u4e0d\u662f", "\u60f3\u4e8b\u60c5", "\u8a8d\u771f"],
            None,
        ),
        (
            "\u6211\u6700\u8fd1\u4e00\u76f4\u5728\u8abf\u4f60\uff0c\u771f\u7684\u6709\u9ede\u7d2f",
            LIVE_TIRED_TEMPLATE_REPLY_TEXT,
            # Same truncation behavior: the clean opening line survives, the banned lines after
            # it (\u6708\u6708\u90fd\u77e5\u9053/\u8c93\u5a18\u4f11\u606f\u7ad9) get dropped rather than triggering full replacement.
            ["\u8abf", "\u7d2f"],
            None,
        ),
        (
            "\u6211\u89ba\u5f97\u4f60\u525b\u525b\u6709\u9ede\u50cf\u6a5f\u5668\u4eba",
            LIVE_MACHINE_TEMPLATE_REPLY_TEXT,
            ["\u624d\u4e0d\u662f", "\u60f3\u4e8b\u60c5", "\u8a8d\u771f"],
            None,
        ),
        (
            "\u966a\u6211\u804a\u4e00\u4e0b",
            CONTROLLED_COMPANION_REPLY_TEXT,
            # Persona recalibration 2026-07-16: the companion fallback is now soft/receptive
            # (\u597d\u5440\u3002\u3002\u3002\u6708\u6708\u4e00\u76f4\u90fd\u5728\u7684\uff0c\u60f3\u804a\u4ec0\u9ebc\uff1f) - warmth shows as presence, not a teasing bargain.
            ["\u6708\u6708", "\u4e00\u76f4\u90fd\u5728", "\u60f3\u804a"],
            ["\u4e00\u76f4\u90fd\u5728", "\u60f3\u804a"],
        ),
        (
            "\u966a\u6211\u804a\u4e00\u4e0b",
            "\u7b2c\u4e00\u6bb5\u9084\u5728\u8aaa\n\u7b2c\u4e8c\u6bb5\u7e7c\u7e8c\u8aaa\n\u7b2c\u4e09\u6bb5\u53c8\u52a0\u4e00\u9ede\n\u7b2c\u56db\u6bb5\u518d\u7e7c\u7e8c\u62c9\u9577",
            # Synthetic length-only stress case with no real topic words; truncation keeps the
            # first two (content-clean) lines rather than swapping in the canned fallback.
            ["\u7b2c", "\u6bb5"],
            None,
        ),
        (
            "\u90a3\u6642\u5019\u53ea\u662f\u6e2c\u8a66",
            LONG_SOCIAL_REPLY_TEXT,
            ["\u5c31\u77e5\u9053", "\u60e6\u8a18", "\u958b\u5fc3"],
            None,
        ),
    ]
    for prompt, provider_reply, markers, warmth_markers in warm_cases:
        with tempfile.TemporaryDirectory() as tmp:
            provider = FixedProvider(provider_reply)
            runtime = YueYueRuntimeV3(ROOT, provider, state_dir=Path(tmp) / "v3")
            reply = runtime.chat(prompt, response_policy=ChatPolicy())["content"]
            # See the comment on the equivalent assertion above: truncation-fails cases now
            # make one extra regeneration attempt before falling back to canned wording.
            assert provider.calls in (1, 2), (prompt, provider.calls)
            assert runtime.state.workflow is None, prompt
            _assert_warm_contextual_chat_reply(prompt, reply, markers, warmth_markers=warmth_markers)

    test_context_cases = [
        "\u90a3\u6642\u5019\u53ea\u662f\u6e2c\u8a66",
        "\u53ea\u662f\u6e2c\u8a66",
    ]
    for prompt in test_context_cases:
        with tempfile.TemporaryDirectory() as tmp:
            provider = FixedProvider(WORDY_TEST_CONTEXT_REPLY_TEXT)
            runtime = YueYueRuntimeV3(ROOT, provider, state_dir=Path(tmp) / "v3")
            reply = runtime.chat(prompt, response_policy=ChatPolicy())["content"]
            assert provider.calls in (1, 2), (prompt, provider.calls)
            assert runtime.state.workflow is None, prompt
            _assert_test_context_chat_reply(reply)


def _check_project_meta_context_is_suppressed_for_chat() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        normal_path = Path(tmp) / "short_context.json"
        buffer = ShortContextBuffer(str(normal_path), max_turns=5)
        buffer.observe_turn("chat", PROJECT_META_CONTEXT_TEXT)
        buffer.update_last_assistant("chat", PROJECT_META_REPLY_TEXT)
        rendered = buffer.render_for_turn("chat", "\u966a\u6211\u804a\u4e00\u4e0b")
        for phrase in CHAT_META_FORBIDDEN:
            assert phrase.casefold() not in rendered.casefold(), rendered

        v3_path = Path(tmp) / "v3_short_context.json"
        store = ShortContextStore(v3_path, max_turns=6)
        store.append("telegram", ContextTurn("user", PROJECT_META_CONTEXT_TEXT))
        store.append("telegram", ContextTurn("assistant", PROJECT_META_REPLY_TEXT))
        compiler = ContextCompiler(ROOT, store)
        messages = compiler.compile_turn(TurnEnvelope("telegram", "\u966a\u6211\u804a\u4e00\u4e0b", TurnMode.CHAT))
        history = "\n".join(str(item.get("content", "")) for item in messages[:-1] if item.get("role") != "system")
        for phrase in CHAT_META_FORBIDDEN:
            assert phrase.casefold() not in history.casefold(), history

summary = outbox_operational_summary()
_check_short_context_sanitizers()
_check_plain_greetings_do_not_auto_sticker()
_check_testing_phrases_stay_chat()
_check_provider_failure_reply_safety()
_check_chat_response_tightness_policy()
_check_project_meta_context_is_suppressed_for_chat()

print("YueYue Operational Stability")
print(f"Outbox jobs: {summary['jobs']}")
print(f"Sent: {summary['sent']}")
print(f"Failed: {summary['failed']}")
print(f"Pending: {summary['pending']}")
if summary["last_error"]:
    print(f"Last error: {summary['last_error']}")
print(f"Outbox file: {summary['file']}")
print("Status: pass")
