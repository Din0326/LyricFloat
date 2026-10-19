# L2 spike（語音錨點可行性驗證）

驗證三件事：Python 能否只擷取播放器程序的聲音、faster-whisper 在本機 CPU 的速度與資源、15 秒片段比對出的偏移準不準。不是正式功能，也不會被 App 載入。

```bash
.venv/Scripts/python -m pip install -e ".[l2]"
.venv/Scripts/python tools/l2_spike/capture.py Spotify.exe
.venv/Scripts/python tools/l2_spike/run.py --app Spotify.exe --windows 10 --model base
```

- `capture.py`：WASAPI process loopback（Windows 10 2004 以上），只錄指定程式（含子程序）的聲音，轉成 16 kHz 單聲道
- `recognize.py`：辨識子程序，從 stdin 讀音訊、輸出含逐字時間的 JSON
- `run.py`：每 15 秒一窗，比對預期位置前後 15 秒內的歌詞行，輸出偏移估計與資源用量

音訊只在記憶體內，不寫檔；輸出只有數字，不印歌詞或辨識文字。模型第一次執行時下載到 `%LOCALAPPDATA%\LyricFloat\models`（base 約 142 MB）。

## L3（`tools/l3_spike/`）

```bash
.venv/Scripts/python -m pip install -e ".[l2,l3]"
.venv/Scripts/python tools/l3_spike/run.py --app Spotify.exe --songs 2
```

等下一首歌開始，整首錄下（只在記憶體），再於子程序以 Demucs 分離人聲、stable-ts 把已知歌詞強制對齊（沿用 base 模型），分別比較「分離後人聲」與「原始混音」對齊出的行開始時間和 LRC 的差距，並列出各步驟耗時、CPU 與記憶體。Demucs 模型第一次執行時下載到 `%LOCALAPPDATA%\LyricFloat\models\torch`（約 80 MB）。共用的小函式在 `common.py`。

## MV 開唱點（`mv_start.py`）

驗證「長度差超過 10 秒、等待對齊開唱的 MV」能不能在第一次播放時邊播邊聽找到開唱點、要多久。

```bash
.venv/Scripts/python tools/l2_spike/mv_start.py --app Chrome --songs 3
```

- 只處理 App 會等待對齊開唱的歌（同步版本、長度差 > 10 秒）；其他歌略過
- 從頭不間斷錄播放器的聲音，切成 10 秒片段（各帶前一段最後 2 秒），只把可能含開唱點的片段（第一句原始時間加上 0 到長度差、前後 3 秒）交給 L3 工作程序對齊前 3 句；兩句有把握且相差 1 秒內即算找到
- 聽到第一句開唱時按 **Ctrl+Alt+M** 當標準答案（App 的校對模式要關著；被占用時用 `--key` 換）
- 輸出每段的信心、推算偏移、處理時間（載入、分離、對齊）與記憶體峰值，以及每首的誤差與「開唱後幾秒找到」（含模型常駐時的估計）；寫入 `reports/raw/mv_start_<時間>.jsonl`（不進版控），只有數字與歌名，不含歌詞
