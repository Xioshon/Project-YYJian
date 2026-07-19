# 月月 技能 / 連接器 完整目錄(2026-07-16 規劃)

> owner 要求:列出所有值得新增的技能和插件、完整數量;模型要**知道什麼時候調用**(不是只有講到「提醒」才會用);
> 網絡搜尋要**準**;電腦操控體驗要**好**;整體 provider 無關、可發布。
> 原則:實用優先、純本地優先(可發布)、每個技能都要有清楚的「何時用」給模型路由。

## 現狀(要先修的地基)

- **既有 5 個 markdown 技能是孤兒**:`workspace/skills/{safe-computer-use,code-review-lite,debug,telegram,vision}/SKILL.md`
  有 triggers + allowed_tools,但**沒有任何代碼載入**——等於不存在。要嘛接進去用,要嘛廢掉。
- **兩套技能概念要統一**:markdown 技能(任務模式行為指引)vs `skill_engine.py`(聊天模式能力 handler)。
  統一成:**一個技能目錄,模型看到全部、自己判斷何時用**。
- **web_search 太弱**:DuckDuckGo 抓 3 條原始結果、不抓正文、不合成。要重做成「抓 top 結果正文→合成答案+附來源」。
- **路由「知道何時用」**:現在只靠關鍵詞預篩,只有講到「提醒」才觸發。要升級成**輕量意圖判斷**,
  讓「冷不冷」能想到查天氣、「X 是什麼」能想到搜索——不靠死關鍵詞。
- **電腦操控體驗一般**(owner 反饋):get_screen_ui→click 的可靠度、回饋要改善。

---

## 技能目錄(可玩性 + 生產力 + 資訊 + 陪伴)

圖例:✅已做　🔷純本地(可發布無依賴)　🌐需網絡/API　🧠靠模型　依賴標註在後。

### A. 生產力(共 15)
1. ✅🔷 set_reminder — 定時提醒(相對/絕對時間)
2. ✅🔷 list_reminders — 看有哪些提醒
3. ✅🔷 cancel_reminder — 取消提醒
4. 🔷 recurring_reminder — 週期提醒(每天/每週吃藥、喝水)
5. 🔷 countdown_timer — 計時器 / 番茄鐘(25 分鐘到了叫我)
6. 🔷 add_note — 快速備忘(「幫我記一下:牛奶沒了」)
7. 🔷 list_notes / search_notes — 看/搜備忘
8. 🔷 todo_add / todo_list / todo_done — 待辦清單
9. 🔷 quick_calc — 算術/百分比/開根號
10. 🔷 unit_convert — 單位換算(長度/重量/溫度/容量)
11. 🌐 currency_convert — 匯率換算(需即時匯率)
12. 🔷 expense_log — 記帳(「午餐 60 塊」)
13. 🔷 expense_summary — 本月/本週花了多少、分類
14. 🔷 world_clock — 世界時鐘(紐約現在幾點)
15. 🔷 date_calc — 日期計算(距離考試還有幾天、某日星期幾)

### B. 資訊(共 8)
16. 🌐 web_search（重做求準）— 搜索並合成答案+附來源
17. 🌐 weather — 天氣(今天/明天、某地);路由:「冷不冷/要帶傘嗎」也要能想到
18. 🌐 news_headlines — 今日新聞摘要
19. 🧠 translate — 翻譯(模型即可)
20. 🌐 summarize_url — 把連結內容總結(已有 read_url_context 基礎)
21. 🌐 define_lookup — 查一個詞/概念(百科)
22. 🌐 price_check — 股價/幣價/金價
23. 🧠🌐 answer_factual — 事實問答:先判斷要不要搜索,能答就答、不確定就搜

### C. 可玩性 / 陪玩(共 11)
24. 🔷 dice_roll — 擲骰(D6/D20/自訂)
25. 🔷 coin_flip — 拋硬幣
26. 🔷 decide_pick — 幫我決定(從幾個選項選)
27. 🔷 eat_decider — 今天吃什麼(主題化的趣味選擇)
28. 🔷 daily_fortune — 今日運勢(俏皮、每天一次)
29. 🔷 draw_lots — 抽籤 / 隨機抽人
30. 🧠 number_guess — 猜數字遊戲(可多回合)
31. 🧠 word_chain — 成語接龍
32. 🧠 riddle — 猜謎 / 腦筋急轉彎
33. 🔷 rock_paper_scissors — 猜拳
34. 🧠 story_together — 接龍講故事 / 角色扮演小劇場

### D. 陪伴(綁記憶層,共 4)
35. 🔷 remember_this — 主人主動說「記住X」→ 寫進長期記憶(帶原話)
36. 🔷 recall_this — 「你記得X嗎」→ 明確查記憶(現有隱式召回的顯式版)
37. 🔷 mood_diary — 今日心情記錄,累積成情緒曲線
38. 🔷 anniversary — 紀念日/生日登記 + 到期主動祝(綁 reminders + memory)

### E. 媒體(共 1 新)
39. 🌐 generate_image — 生成圖片發給主人(角色一致性難,優先級低,先做場景/表情類)

**技能小計:39 個(已做 3,待做 36)。**

---

## 連接器 / 插件(外部整合,共 8)

大眾 agent 的「plugins」= 連外部服務。對一個**可發布的私人 agent**,正解不是硬編 20 個整合,而是:

40. **MCP client(最重要)** — 支援連接任何 MCP server。一個抽象,自動獲得下面一票能力,
    也是發布後別人自己接自己服務的標準方式。做這一個 = 抵十個。
41. 🌐 weather 連接器(OpenWeather 類,或免 key 的 wttr.in)— 第一方,常用
42. 🌐 calendar 連接器(Google Calendar 讀/寫)— 需 OAuth
43. 🌐 email 連接器(Gmail 讀/草稿)— 需 OAuth、且發送要許可
44. 🌐 music 控制(Spotify / 本地播放器)
45. 🌐 smart_home(Home Assistant)
46. 🌐 notion / 筆記 app 同步
47. 🌐 search API 連接器(Serper/Brave/Tavily — 讓 web_search 更準的後端選項)

**連接器小計:8 個(MCP client 一個就覆蓋大半)。**

---

## 也要修的既有能力(體驗)

- **web_search 重做**:抓 top-N 結果正文 → 合成簡答 + 列來源;查詢改寫;失敗降級。可插後端(DDG 免費 / Serper/Tavily 更準)。
- **電腦操控體驗**:get_screen_ui 命名元素優先、click 命中率、每步回饋、卡住時誠實停。把 safe-computer-use 技能真正接進任務模式。
- **既有 5 個孤兒技能**:接進運行時(任務模式看到 triggers + 建議 allowed_tools + 行為指引),或統一到新技能目錄。

---

## 統一路由設計(「知道何時用」的核心)

問題:現在只有關鍵詞命中才觸發技能;要讓模型像人一樣「該用就用」。
方案(成本可控三層):
1. **確定信號**:媒體/明確關鍵詞 → 直接觸發(零成本)。
2. **輕量意圖**:訊息含疑問/請求語氣時,一次便宜的模型判斷「這需要動用某個技能嗎?哪個?」
   ——技能目錄的 `when` 描述就是給它讀的。純閒聊不觸發。
3. **兜底**:判斷不了就當閒聊,絕不硬套。

---

## 總數
**技能 39(已做 3、待做 36)+ 連接器 8 = 47 個能力點**,外加 3 項既有能力重做(web_search / 電腦操控 / 孤兒技能接線)。

## 建議建造順序(給 owner 選)
- **第一批(可玩性立竿見影 + 純本地零依賴)**:dice/coin/decide/eat_decider/fortune/rps + note + timer + calc/unit + date_calc —— 一口氣十幾個,聊天立刻好玩、有用,不碰網絡不燒錢。
- **第二批(生產力 + 求準)**:web_search 重做 + weather + todo + expense + recurring_reminder + world_clock。
- **第三批(擴展性)**:統一路由「知道何時用」+ MCP client + 既有孤兒技能接線 + 電腦操控體驗。
- **第四批**:陪伴類(remember/anniversary/mood)、翻譯、其餘連接器。
