from __future__ import annotations

import re
import unicodedata


PROMPT_SHAPED_MARKERS = [
    "你是月月",
    "使用者原句",
    "已驗證時間",
    "已验证时间",
    "必須包含",
    "必须包含",
    "限制：",
]



CURRENT_TIME_CLAIM_RE = re.compile(
    r"(?:現在|现在|目前|current).{0,12}(?:是|time)?.{0,8}20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}",
    re.I,
)



WORKFLOW_APPROVAL_MARKERS = [
    "這一步需要你點頭",
    "这一步需要你点头",
    "你回「可以」",
    "你回\"可以\"",
    "我才能繼續",
    "我才能继续",
    "接著剛才的任務",
    "接着刚才的任务",
    "permission",
    "execute_command",
]



WORKFLOW_ERROR_MARKERS = [
    "\u6a21\u578b\u670d\u52d9",
    "\u6a21\u578b\u670d\u52a1",
    "\u66ab\u6642\u6c92\u6709\u6b63\u5e38\u56de\u61c9",
    "\u6682\u65f6\u6ca1\u6709\u6b63\u5e38\u56de\u5e94",
    "\u4efb\u52d9\u9032\u5ea6",
    "\u4efb\u52a1\u8fdb\u5ea6",
    "\u4fdd\u7559\u4efb\u52d9",
    "\u4fdd\u7559\u4efb\u52a1",
    "\u6c92\u6709\u7e7c\u7e8c\u4e82\u64cd\u4f5c",
    "\u6ca1\u6709\u7ee7\u7eed\u4e71\u64cd\u4f5c",
]



PROJECT_RUNTIME_META_MARKERS = [
    "agent runtime",
    "runtime",
    "debug",
    "bot \u6a21\u5f0f",
    "\u5beb\u500b bot",
    "\u5199\u4e2a bot",
    "workflow",
    "PromptCompiler",
    "provider",
    "v3",
    "\u6a21\u578b\u670d\u52d9",
    "\u6a21\u578b\u670d\u52a1",
    "\u4efb\u52d9\u9032\u5ea6",
    "\u4efb\u52a1\u8fdb\u5ea6",
    "\u958b\u767c",
    "\u5f00\u53d1",
    "\u7406\u60f3\u7684\u6708\u6708",
    "\u5c0d\u7684\u6708\u6708",
    "\u5bf9\u7684\u6708\u6708",
    "\u56de\u6536\u7ad9",
]



DEVELOPMENT_REQUEST_MARKERS = [
    "debug",
    "runtime",
    "workflow",
    "PromptCompiler",
    "provider",
    "v3",
    "\u4fee",
    "\u958b\u767c",
    "\u5f00\u53d1",
    "\u4ee3\u78bc",
    "\u4ee3\u7801",
    "\u7a0b\u5f0f",
    "\u7a0b\u5e8f",
    "\u6a21\u578b",
]



UNSTABLE_SOCIAL_META_MARKERS = [
    "\u4efb\u52d9\u72c0\u614b\u4e0d\u898b\u4e86",
    "\u4efb\u52a1\u72b6\u6001\u4e0d\u89c1\u4e86",
    "\u907f\u514d\u7e7c\u7e8c\u8aa4\u64cd\u4f5c",
    "\u907f\u514d\u7ee7\u7eed\u8bef\u64cd\u4f5c",
    "\u7b2c\u4e09\u6b21",
    "\u7b2c\u4e09\u6b21\u4e86",
    "\u65e9\u4e0a\u5230\u665a\u4e0a\u90fd\u6253\u904e\u62db\u547c",
    "\u65e9\u4e0a\u5230\u665a\u4e0a\u90fd\u6253\u8fc7\u62db\u547c",
    "\u8166\u888b\u5361\u5728\u958b\u6a5f\u756b\u9762",
    "\u8111\u888b\u5361\u5728\u5f00\u673a\u753b\u9762",
    "\u8907\u8b80\u6a5f",
    "\u590d\u8bfb\u673a",
    "\u7b2c\u4e09\u676f",
    "\u53c8\u4f86\u4e00\u6b21",
    "\u53c8\u6765\u4e00\u6b21",
    "\u53c8\u8aaa\u4f60\u597d",
    "\u53c8\u8bf4\u4f60\u597d",
    "\u50ac\u7720\u66f2",
    "\u9019\u6b21\u662f\u771f\u7684\u65e9\u4e0a\u597d",
    "\u8fd9\u6b21\u662f\u771f\u7684\u65e9\u4e0a\u597d",
    "\u5f85\u6a5f\u6a21\u5f0f",
    "\u5f85\u673a\u6a21\u5f0f",
    "\u9700\u8981\u6211\u5e6b\u4f60",
    "\u9700\u8981\u6211\u5e2e\u4f60",
    "\u6574\u7406\u4eca\u5929\u7684\u4efb\u52d9",
    "\u6574\u7406\u4eca\u5929\u7684\u4efb\u52a1",
    "\u4eca\u5929\u7684\u4efb\u52d9\u548c\u884c\u7a0b",
    "\u4eca\u5929\u7684\u4efb\u52a1\u548c\u884c\u7a0b",
    "\u558a\u9019\u9ebc\u751c",
    "\u558a\u8fd9\u4e48\u751c",
    "\u807d\u5230\u4e86\u5566",
    "\u542c\u5230\u4e86\u5566",
    "\u4e3b\u4eba\uff5e\u62ff\u53bb",
    "\u525b\u9192\u5c31",
    "\u521a\u9192\u5c31",
    "\u8166\u888b\u958b\u6d1e",
    "\u8111\u888b\u5f00\u6d1e",
    "\u5594\uff0c\u9019\u5f35\u6b78\u4f60",
    "\u5594\uff0c\u8fd9\u5f20\u5f52\u4f60",
    "\u9019\u5f35\u9084\u7b97\u80fd\u770b",
    "\u8fd9\u5f20\u8fd8\u7b97\u80fd\u770b",
    "\u6211\u518d\u88dc\u767c\u4e00\u6b21",
    "\u6211\u518d\u8865\u53d1\u4e00\u6b21",
    "\u6708\u6708\u807d\u8457",
    "\u6708\u6708\u542c\u7740",
    "\u770b\u5230\u4f60",
    "\u770b\u5230\u4f60\u4e86",
    "\u8aaa\u6b63\u4e8b",
    "\u8bf4\u6b63\u4e8b",
    "\u6c92\u8ff7\u8def",
    "\u6ca1\u8ff7\u8def",
    "\u55b5\u9019\u9ebc\u751c",
    "\u55b5\u8fd9\u4e48\u751c",
    "\u55e8\u5230\u5566",
    "\u5225\u88dd\u8def\u904e",
    "\u5225\u88c5\u8def\u8fc7",
    "\u6293\u5230\u4e00\u96bb\u6253\u62db\u547c\u7684\u4eba",
    "\u6293\u5230\u4e00\u53ea\u6253\u62db\u547c\u7684\u4eba",
    "\u5225\u558a\u90a3\u9ebc\u6b63\u7d93",
    "\u522b\u558a\u90a3\u4e48\u6b63\u7ecf",
    "\u627e\u6708\u6708\u554a",
    "\u4eca\u5929\u5148\u653e\u4f60\u9032\u4f86",
    "\u4eca\u5929\u5148\u653e\u4f60\u8fdb\u6765",
    "\u60f3\u804a\u5929\u9084\u662f\u6709\u4efb\u52d9",
    "\u60f3\u804a\u5929\u8fd8\u662f\u6709\u4efb\u52a1",
    "\u6d63\u72b2\u30bd\u5f35\u6e1a\u4e00\u6b21",
    "\u7b2c\u4e09\u93c8",
    "\u5148\u5225\u786c\u6490",
    "\u5225\u786c\u6490",
    "\u5225\u81ea\u5df1\u61cb\u8457",
    "\u8aaa\u7d66\u6211\u807d",
    "\u8bf4\u7ed9\u6211\u542c",
    "\u966a\u4f60\u4e00\u4e0b",
    "\u6211\u966a\u4f60",
    "\u6211\u6703\u966a\u4f60",
    "\u6211\u4f1a\u966a\u4f60",
    "\u6211\u5728\u9019\u88e1",
    "\u6211\u5728\u8fd9\u91cc",
    "\u4f60\u7d2f\u4e86\u5c31\u4f11\u606f\u4e00\u4e0b",
    "\u4e3b\u4eba\u8f9b\u82e6\u4e86",
    "\u6708\u6708\u6703\u966a\u8457\u4f60",
    "\u6708\u6708\u4f1a\u966a\u7740\u4f60",
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
    "\u6d4b\u8bd5\uff01",
    "\u9592\u804a\u6a21\u5f0f",
    "\u95f2\u804a\u6a21\u5f0f",
    "\u8cbc\u5716\u9b25\u4e5f\u884c",
    "\u8d34\u56fe\u6597\u4e5f\u884c",
    "\u53c8\u628a\u6708\u6708\u62ce\u51fa\u4f86\u76ef\u5834",
    "\u4f60\u5148\u4e1f\u4e00\u53e5\u904e\u4f86",
    "\u6211\u4e0d\u8dd1",
    "\u5c11\u8aaa\u6708\u6708\u5c0f\u6c23",
    "\u5c11\u8bf4\u6708\u6708\u5c0f\u6c14",
    "\u5c0f\u6c23",
    "\u5c0f\u6c14",
    "\u6562\u5acc\u68c4",
    "\u6562\u5acc\u5f03",
    "\u6c92\u4e0b\u6b21",
    "\u6ca1\u4e0b\u6b21",
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
    "\u5148\u6162\u4e00\u9ede",
    "\u5148\u6162\u4e00\u70b9",
    "\u5148\u8b1b\u4e00\u9ede",
    "\u5148\u8bf4\u4e00\u70b9",
    "\u4eca\u5929\u5148\u5b88\u8457\u4f60",
    "\u4eca\u5929\u5148\u5b88\u7740\u4f60",
    "\u55ef\uff0c\u5c31\u9019\u5f35",
    "\u55ef\uff0c\u5c31\u8fd9\u5f20",
    "\u9019\u5f35\u4e5f\u53ef\u4ee5",
    "\u8fd9\u5f20\u4e5f\u53ef\u4ee5",
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
    "\u5c3e\u5df4\u8f15\u8f15",
    "\u5c3e\u5df4\u8f7b\u8f7b",
    "\u8033\u6735\u8f15\u8f15",
    "\u8033\u6735\u8f7b\u8f7b",
    "\u722a\u5b50\u8f15\u8f15",
    "\u722a\u5b50\u8f7b\u8f7b",
]



TIME_FACT_REPLY_RE = re.compile(
    r"20\d{2}-\d{2}-\d{2}.{0,20}(?:\u661f\u671f|\u4eca\u5929|\u73fe\u5728|\u73b0\u5728)",
    re.I,
)



def _is_prompt_shaped_context(text: str) -> bool:
    value = str(text or "")
    hits = sum(1 for marker in PROMPT_SHAPED_MARKERS if marker in value)
    return hits >= 2



def _has_current_time_claim(text: str) -> bool:
    return bool(CURRENT_TIME_CLAIM_RE.search(str(text or "")))



def _has_workflow_approval_claim(text: str) -> bool:
    value = str(text or "").casefold()
    return any(marker.casefold() in value for marker in WORKFLOW_APPROVAL_MARKERS)



def _has_workflow_error_claim(text: str) -> bool:
    value = str(text or "").casefold()
    return any(marker.casefold() in value for marker in WORKFLOW_ERROR_MARKERS)



def _has_project_runtime_meta(text: str) -> bool:
    value = str(text or "").casefold()
    return any(marker.casefold() in value for marker in PROJECT_RUNTIME_META_MARKERS)



def _asks_about_development(text: str) -> bool:
    value = str(text or "").casefold()
    return any(marker.casefold() in value for marker in DEVELOPMENT_REQUEST_MARKERS)



def _has_unstable_social_meta(text: str) -> bool:
    value = str(text or "").casefold()
    return any(marker.casefold() in value for marker in UNSTABLE_SOCIAL_META_MARKERS)



def _has_time_fact_reply(text: str) -> bool:
    value = str(text or "")
    return bool(TIME_FACT_REPLY_RE.search(value))



def _is_plain_greeting_text(text: str) -> bool:
    value = str(text or "").casefold()
    value = re.sub(r"[\s\u3000\uff0c,.\u3002!！?？~～]+", "", value)
    for name in ["\u6708\u6708", "\u93c8\u581f\u6e40"]:
        value = value.replace(name.casefold(), "")
    return value in {
        "hi",
        "hello",
        "\u55e8",
        "\u4f60\u597d",
        "hi\u4f60\u597d",
        "\u6d63\u72b2\u30bd",
        "hi\u6d63\u72b2\u30bd",
    }



def _sanitize_context_text(text: str, *, role: str = "", allow_project_meta: bool = False) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ""
    if _is_prompt_shaped_context(value):
        return ""
    if not allow_project_meta and _has_project_runtime_meta(value):
        return ""
    if role == "assistant" and (
        _has_current_time_claim(value)
        or _has_workflow_approval_claim(value)
        or _has_workflow_error_claim(value)
        or _has_unstable_social_meta(value)
        or _has_time_fact_reply(value)
    ):
        return ""
    return value


_BENIGN_TESTING_CHAT_NOTES = {
    "測試",
    "测试",
    "測試一下",
    "测试一下",
    "只是測試",
    "只是测试",
    "只是測試一下",
    "只是测试一下",
    "那時候只是測試",
    "那时候只是测试",
    "剛剛只是測試",
    "刚刚只是测试",
    "剛才只是測試",
    "刚才只是测试",
    "前面只是測試",
    "前面只是测试",
    "test",
    "ping",
}

# Phrases long/specific enough to safely match as a prefix, not just an exact whole message.
# Bare "測試"/"测试"/"測試一下"/"测试一下"/"test"/"ping" stay exact-only - as a prefix they would
# wrongly swallow any real sentence that merely starts with the word "test" (e.g. "測試帳號可以
# 登入嗎？幫我看一下畫面" is a genuine task request, not a benign remark). Only the "只是.../那時候
# 只是.../剛剛只是..." phrasings carry enough of a "just testing, nothing more" qualifier to be
# safe as a prefix.
_BENIGN_TESTING_NOTE_BARE_WORDS = {"測試", "测试", "測試一下", "测试一下", "test", "ping"}
_BENIGN_TESTING_NOTE_PREFIXES = tuple(
    phrase for phrase in _BENIGN_TESTING_CHAT_NOTES if phrase not in _BENIGN_TESTING_NOTE_BARE_WORDS
)


def is_benign_testing_note(text: str) -> bool:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    value = re.sub(r"[\s　，,.。!！?？~～「」『』\"'`]+", "", value)
    value = value.replace("月月", "")
    if value in _BENIGN_TESTING_CHAT_NOTES:
        return True
    # Real chat often keeps going past the testing remark itself ("剛剛只是測試，繼續正常聊天
    # 吧"), so an exact-whole-message check misses it - a testing-remark prefix is still a
    # testing remark even when the owner keeps talking after it.
    return value.startswith(_BENIGN_TESTING_NOTE_PREFIXES)
