# LyricFloat

Windows 桌面懸浮歌詞工具（個人使用）。目前進度：MVP（v1.0）驗收中；**v1.1 功能已完成**（播放控制、釘選播放器、全域快捷鍵、手動搜尋與綁定、卡拉 OK 填色），待命中率回歸閘門。完整規格見 [docs/SPEC.md](docs/SPEC.md)。

能做的事：

- 透過 Windows 系統媒體會話（SMTC）偵測播放器的歌名、歌手、專輯、時長、進度與播放狀態（Spotify、Chrome 的 YouTube Music）
- 歌詞來源：本地快取 → [LRCLIB](https://lrclib.net) → 網易雲，依歌曲語言決定順序，依規格評分挑選
- 簡體歌詞與歌名一律轉成台灣正體（OpenCC），日文與韓文不轉換
- 英文歌附繁中翻譯行；同一首歌的多個版本會比對時間軸，自動避開整首偏移的版本（L1）
- 透明、置頂、不出現在工作列的雙行歌詞視窗；鎖定時滑鼠穿透，解鎖時可拖曳、拖邊縮放
- 每首歌可微調偏移並記住；本地快取讓重播與斷網時也能顯示
- 「這首歌詞有誤」匯出診斷資料、校對模式量時間軸誤差

## 需求

- Windows 10 21H2 以上或 Windows 11
- 從原始碼執行：Python 3.11 以上（開發時使用 3.12）

## 使用免安裝版

1. 解壓縮 `LyricFloat-<版本>-windows-portable.zip`（由 `tools/build.py` 產生在 `dist/`）
2. 執行資料夾內的 `LyricFloat.exe`

免安裝版開啟「自動時間軸校正」時，會先在背景下載執行環境到 `%LOCALAPPDATA%\LyricFloat\runtime`（python.org 的嵌入版 Python 與 PyTorch 等套件，約 250 MB，安裝後約 1 GB）。版本與檢查碼固定在 `src/lyricfloat/align/runtime_lock.json`，下載後逐一核對；要更新時用 `tools/lock_l3.py` 重新產生。

要開機自動啟動：在設定頁「一般」勾選「Windows 登入時自動啟動」，會在「啟動」資料夾建立捷徑（不寫登錄檔）；取消勾選即刪除捷徑。

## 從原始碼安裝與執行

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,measure,build]"
```

`dev` 是測試用套件、`measure` 是量測工具、`build` 是打包工具；只要執行 App 可以只裝 `pip install -e .`。

```bash
.venv/Scripts/pythonw -m lyricfloat
```

用 `pythonw` 不會開主控台視窗。也可以按 Win+R 執行 `<專案路徑>\.venv\Scripts\pythonw.exe -m lyricfloat`。

| 參數 | 用途 |
| --- | --- |
| `--debug` | 詳細日誌，同時輸出到終端機（要搭配 `python` 而非 `pythonw`） |
| `--demo` | 不連網、不讀播放器，用展示文字跑一段循環的假時間軸，檢查視窗外觀 |

## 操作

- **滑鼠停在歌詞上**（解鎖時，約 0.2 秒）：出現半透明底色與工具列
  - 上一首／播放・暫停／下一首：控制目前跟隨的播放器；沒有跟隨的播放器時控制 Windows 目前的播放器（例如暫停中的 Spotify），完全沒有播放器時停用
  - 偏移 −／＋：歌詞提前／延後 0.1 秒，可按住連續調整，會記在這首歌
  - 對齊開唱：只在播放長度和歌詞版本不同的歌（例如 YouTube Music 的 MV）出現，見下方「MV 版」
  - A−／A+：字級
  - 鎖定：滑鼠點擊改為穿透到下方視窗
  - 設定、✕（隱藏，可從系統列叫回）
- **拖曳**歌詞可移動，**拖邊緣**可縮放；位置與大小會記住
- **系統列圖示**（♪）右鍵：顯示歌詞、鎖定歌詞、OBS 視窗、跟隨播放器、搜尋歌詞／換版本…、編輯這首歌詞…、這首歌詞有誤…、開啟診斷資料夾、校對模式、設定…、結束
- 所有歌詞來源都無法連線時，系統列圖示出現紅點
- **全域快捷鍵**（任何程式在前景都有效，可在設定改）：顯示／隱藏 Ctrl+Alt+H、鎖定／解鎖 Ctrl+Alt+L、歌詞提前 Ctrl+Alt+←、延後 Ctrl+Alt+→（按住可連續調整，視窗右上角短暫顯示目前偏移）、對齊開唱 Ctrl+Alt+↓
- **MV 版**（播放長度和所有歌詞版本都差 3 秒以上，例如 MV 前後多了劇情）：沒有長度相符的版本時，改用歌名歌手相符、長度不同的版本。開啟自動時間軸校正、從頭播放時，會邊播邊聽找第一句開唱的位置，找到就自動對齊，大約在開唱後 10 秒（和它的時間軸差 2 秒內就不動，等播完後的自動校正）；找到前先照它的時間軸逐行顯示，左上角標「自動對齊開唱中…」。沒聽到開唱點或沒在聽時改用它的時間軸，並提示可以在開唱時按 Ctrl+Alt+↓（之後歌詞落後時再按一次，下一句就從按下時開始）。對齊結果記在這首歌，下次播放直接同步；完整播完一次後，L3 會從開唱點附近開始用實際音訊校正。手動綁定長度不同的版本也一樣。粵語歌目前聽不出開唱點
- **YouTube 影片**（瀏覽器播放）：標題常把歌手、歌名、Official MV、動態歌詞、#標籤混在一起，歌手欄也常是上傳頻道。一般比對找不到時，會用整理過的標題全文搜尋各來源，候選的歌名和歌手都出現在影片標題裡才採用。仍找不到時可改用 YouTube Music（music.youtube.com 對歌曲回報乾淨的歌名歌手）或「搜尋歌詞／換版本」手動綁定
- **搜尋歌詞／換版本**：自動挑到的版本不對時，用歌名與歌手搜尋各來源的全部版本（瀏覽器影片已找到歌詞時，預填找到的那首歌的歌名與歌手；沒找到時預填完整影片標題、歌手留空）。歌手欄留空時，歌名欄整段當關鍵字搜尋，歌名和歌手都出現在文字裡的版本排最前面，不分先後（例如「自然醒 林宥嘉」或「林宥嘉 自然醒」）；選取可預覽，按「套用並綁定」後這首歌之後都用這個版本（偏移歸零）；「解除綁定」改回自動挑選
- **直向顯示**（設定「外觀」→排列方向）：兩欄由上往下讀，目前句在左、翻譯或下一句在右；中日韓文字直立、英文轉 90 度；橫向和直向各自記住視窗位置與大小
- **OBS 視窗**：另開一個有標題列的一般視窗，純色背景（預設綠色，可在設定改），和懸浮歌詞同步顯示；在 OBS 用「視窗擷取」選「LyricFloat 歌詞（OBS）」再加「色度去背」。兩行文字都是不透明，去背比較乾淨
- **編輯這首歌詞**：逐行改時間與文字、增刪行，或用「貼上整首歌詞…」一次貼上；邊聽邊選行按「設為目前播放位置」可以自己對時，正在播的行會標淡藍色。「同時調整所有歌詞的時間」（預設勾選）勾著時，改任一行的時間（點兩下修改、±0.1 秒、設為目前播放位置），其他行都跟著加減同樣秒數，適合整首一起早或晚；要逐行對時或只改某一句，先取消勾選。儲存後這首歌改用「本地編輯版」（偏移併入時間），自動比對不會蓋掉；「還原為原始版本」回到第一次編輯前。只貼上或改文字、沒動過時間的歌詞會存成純文字編輯版，開啟自動時間軸校正時完整播完一次就會自動對時
- **自動時間軸校正（L3）**：在設定「一般」開啟後，完整播完一首有歌詞的歌（同步或純文字都可以），會在背景用實際音訊校正時間軸並產生逐字時間；只有純文字的歌因此也能同步顯示，之後播這首歌時間更準、卡拉 OK 逐字填色；校正過的歌在工具列顯示「已自動校正」，點一下還原原始時間軸（之後不再校正）。只錄播放器的聲音、暫存本機、處理完即刪；中途暫停或拖動的歌不處理。從原始碼執行需先 `pip install -e ".[l2,l3]"`
- **跟隨播放器**：預設「自動」跟隨 Windows 目前播放的播放器；沒在播放的播放器不會被切過去顯示（啟動時暫停中的 Spotify、瀏覽器兩支影片之間的停頓都不會跳出它的歌詞）。同時開多個播放器時可釘選其中一個，其他播放器不再影響顯示（設定會保存）

設定頁：

| 分頁 | 內容 |
| --- | --- |
| 一般 | 開機自動啟動、簡體轉換方式（台灣慣用詞／只轉字）、自訂詞彙表、自動時間軸校正（預設關閉，開啟前會說明並詢問） |
| 外觀 | 字型、字級、顏色、描邊、陰影、對齊、排列方向（橫向兩行／直向兩欄）、換句動畫、顯示翻譯、卡拉 OK 填色與已唱顏色、OBS 視窗背景色（即時預覽，取消會還原）；可匯入／匯出主題檔（只含本頁設定，沒安裝的字型改用預設） |
| 歌詞來源 | 啟用或停用 LRCLIB、網易雲（停用網易雲則英文歌沒有翻譯） |
| 快捷鍵 | 自訂五組全域快捷鍵（需含 Ctrl、Alt 或 Win；重複或與校對模式的 Ctrl+Alt+M 相同時無法按確定；被其他程式占用時系統列提示） |

視窗顯示的狀態：

| 情況 | 顯示 |
| --- | --- |
| 沒有播放中的會話 | 「等待播放中…」，3 秒後淡出 |
| 查詢中、前奏 | 第一行「歌名 — 歌手」，第二行為第一句歌詞 |
| 有同步歌詞 | 第一行為當前句，依每句時長推估進度以藍色卡拉 OK 填色；第二行為翻譯（英文歌，80% 不透明），沒有翻譯的歌則顯示下一句（60% 不透明） |
| 只有純文字歌詞 | 依歌曲時長等速捲動，左上角標「未同步」 |
| MV 版等長度不同的同步歌詞，正在邊播邊聽找開唱點 | 照來源時間軸逐行顯示，左上角標「自動對齊開唱中…」；找到開唱點後整首一起移 |
| 純音樂 | 「♪ 純音樂」 |
| 找不到或來源全部失敗 | 「歌名 — 歌手」＋「找不到歌詞」 |
| 瀏覽器播放超過 15 分鐘、或沒有歌手的影片（抖音、Instagram 等） | 視為不是歌，不查詢、視窗留白 |
| 疑似 Spotify 廣告（無專輯且短於 60 秒） | 不查詢、視窗留白 |

## 資料存放位置

全部只存在這台電腦，不上傳。

| 位置 | 內容 |
| --- | --- |
| `%APPDATA%\LyricFloat\settings.json` | 設定（不含任何帳號憑證） |
| `%APPDATA%\LyricFloat\vocabulary.txt` | 自訂詞彙表 |
| `%APPDATA%\LyricFloat\test_list.csv` | 測試歌單 |
| `%LOCALAPPDATA%\LyricFloat\cache.sqlite3` | 歌詞快取（含每首歌的偏移、手動綁定的版本與本地編輯版） |
| `%LOCALAPPDATA%\LyricFloat\logs\` | `lyricfloat.log` 滾動日誌、`match.jsonl` 候選評分、`history.jsonl` 播放紀錄、`calibration.jsonl` 校對紀錄、`alignment.jsonl` 自動校正統計 |
| `%LOCALAPPDATA%\LyricFloat\models\` | 自動時間軸校正的模型（第一次處理時下載，約 220 MB） |
| `%LOCALAPPDATA%\LyricFloat\tmp\` | 自動時間軸校正的暫存錄音（處理完即刪，啟動時清掉殘留） |
| `%LOCALAPPDATA%\LyricFloat\reports\` | 「這首歌詞有誤」匯出的診斷檔（含歌詞快照） |

日誌、播放紀錄、校對紀錄與自動校正統計只記中繼資料與數字，不記歌詞內容。

## 測試

```bash
.venv/Scripts/python -m pytest
```

測試資料全部是自編文字，不含真實歌詞。來源契約測試（`tests/test_contracts.py`）預設重播 `tests/contracts/` 的錄製檔、不連網；要對真實端點重新錄製並檢查格式是否改變：

```bash
.venv/Scripts/python -m pytest tests/test_contracts.py --record-contracts
```

錄製時所有歌詞文字會先換成占位字串，並有測試確保錄製檔不含歌詞。

## 驗收與量測工具

| 指令 | 用途 |
| --- | --- |
| `python tools/hit_rate.py candidates` | 從播放紀錄整理候選歌單（`reports/private/歌單候選.xlsx`），在黃底欄勾選「納入」 |
| `python tools/hit_rate.py adopt` | 把勾選的歌加入測試歌單 |
| `python tools/hit_rate.py run` | 批次查詢測試歌單：英文／華語同步命中率、簡體殘留掃描（`reports/private/命中率報告.xlsx`） |
| `python tools/hit_rate.py calibration` | 彙整校對模式的標記：時間軸誤差中位數（`reports/private/校對結果.xlsx`） |
| `python tools/hit_rate.py alignment` | 彙整自動時間軸校正的統計（只有數字）：採用率、華語／英文與原時間的一致程度、耗時（`reports/private/對齊統計.xlsx`） |
| `python tools/title_variants.py run --per-group 12` | 瀏覽器影片標題自測：播放紀錄中找到歌詞的歌套上 YouTube 常見標題格式，以 Chrome 影片重新解析，列出找不到與抓錯歌（`reports/private/title_variants.xlsx`）；`list` 只列出模擬標題，`--only 歌名` 只跑指定的歌 |
| `python tools/measure_perf.py sample --label idle` | 量閒置時的 CPU 與記憶體（`--label playing` 為播放中） |
| `python tools/measure_perf.py soak` | 72 小時長跑：無崩潰、記憶體增長 < 10%，可隨時 Ctrl+C 停止並保留資料 |
| `python tools/measure_smtc.py measure --player spotify` | M1：播放器透過 SMTC 回報進度的頻率與誤差 |
| `python tools/build.py` | 打包免安裝版並檢查大小（< 80 MB） |
| `python tools/gen_char_tables.py` | 升級 OpenCC 後重新產生簡體偵測用的字元表 |

以上指令都在專案根目錄用 `.venv/Scripts/python` 執行。`reports/private/` 的檔案含歌名（屬聽歌紀錄），不進版控。

**校對模式**：系統列開啟「校對模式」後，聽到每句實際開唱的瞬間按 **Ctrl+Alt+M**，每首記 5 行；規格建議抽 20 首。視窗右上角會顯示「校對 n/5」。

## 專案結構

```
src/lyricfloat/
├─ media/        MediaWatcher（SMTC，pywinrt）、時間軸外推、多會話選擇、Spotify 回報特性處理
├─ lyrics/       LyricService（查詢、L1 時間軸共識、翻譯）、LRC 解析、正規化、評分、語言判斷
│  ├─ sources/   可插拔歌詞來源（LyricSource 介面、LRCLIB、網易雲）
│  ├─ pipeline.py  繁簡轉換（OpenCC，用到才載入）
│  ├─ cache.py     SQLite 快取
│  └─ timing.py    時間軸校正掛點（L2、L3 留給 v1.1 以後）
├─ sync/         SyncEngine（純邏輯）與 Qt 計時驅動
├─ ui/           歌詞視窗、工具列、系統列、設定頁、回報對話框、全域快捷鍵
├─ diagnostics/  診斷匯出、播放紀錄、命中率、校對、效能與 SMTC 量測
├─ settings.py   設定讀寫
├─ autostart.py  開機啟動捷徑
└─ app.py        模組接線（只透過 Qt signal 溝通）
packaging/       PyInstaller 設定
tools/           量測、驗收、打包工具
```

## 尚未實作

英文介面、左右交錯佈局。Spotify 歌詞來源暫緩（見規格 FR-13）。

## 參考與授權

架構參考 [Mai-icy/Spotify-lyrics-window](https://github.com/Mai-icy/Spotify-lyrics-window)（GPL-3.0），僅參考架構與行為，未複製任何程式碼。簡體偵測字元表由 [OpenCC](https://github.com/BYVoid/OpenCC)（Apache-2.0）的資料產生。本專案僅供個人使用。
