# SwitchBot Meter Plus 桌面看板

對照 SwitchBot App「智慧大屏溫濕度計」的資訊卡，在 Windows 長掛顯示溫濕度，並支援可設定警報與本機歷史。

## 啟動（開發用，需本機 Python）

```bat
start.bat
```

或：

```powershell
cd switchbot_meter
python app.py
```

相依：`pip install -r requirements.txt`

測試：

```powershell
python -m unittest tests.test_core -v
```

## 打包給桌上電腦（免裝 Python）

在有 Python 的機器上執行：

```bat
build_portable.bat
```

完成後會產生：

| 路徑 | 說明 |
|------|------|
| `dist\SwitchBotMeter\` | **整包複製**到桌上電腦 |
| `dist\SwitchBotMeter_Portable.zip` | 同上，壓縮方便用 USB / 網路搬 |

桌上電腦操作：

1. 解壓 / 複製整個 `SwitchBotMeter` 資料夾（不要只拿 `.exe`）
2. 雙擊 `start.bat` 或 `SwitchBotMeter.exe`
3. 確認 Windows **藍牙已開啟**（需 BLE，Intel/一般 USB 藍牙即可）
4. 設定與歷史會寫在該資料夾的 `config.json`、`data\`

若 SmartScreen 擋下：更多資訊 → 仍要執行（自製未簽章程式常見）。

**不要**指望 Logitech Unifying / DJI / Xbox 接收器當藍牙用。

## 功能

| 區塊 | 內容 |
|------|------|
| HUD | 溫度、濕度、絕對濕度、露點、VPD、電量、RSSI |
| 操作 | 拖曳移動；右鍵選單；`⋯` 設定；`📈` 歷史 |
| 警報 | 多規則、任意指標/條件/門檻、冷卻、解除裕度、Toast |
| 歷史 | 本機 SQLite、時/日/週/月曲線、CSV 匯出／匯入（含 SwitchBot App） |
| 設定 | `config.json`（介面也可改）；顯示頁可勾選 Windows 開機自動啟動 |

## 歷史資料能從哪裡來？

| 來源 | 能拿到什麼 | 條件 |
|------|------------|------|
| **本程式** | 程式執行期間的連續記錄 | 需要本機 **BLE** |
| **SwitchBot App 匯出** | 裝置內約 **68 天**（Meter Plus） | 手機靠近裝置同步後，App 內「匯出資料」 |
| **Hub + 雲端** | 約 **2 年** | 有 Hub，App 圖表頁開啟雲端儲存 |
| **官方 Open API** | **僅即時**溫濕度/狀態 | 有 Hub + Token；**沒有**歷史曲線 API |

官方 **沒有** 公開「用 API 下載完整歷史曲線」的端點。裝置內部歷史主要靠 **App 藍牙同步後匯出**，或 **Hub 上雲**。

本程式會在有 BLE 時自己建庫：`data/history.db`，並可匯出 CSV。

## 警報範例（config.json）

```json
{
  "id": "temp_high",
  "enabled": true,
  "metric": "temperature_c",
  "op": ">=",
  "value": 30.0,
  "label": "高溫"
}
```

可用指標：`temperature_c`、`humidity_pct`、`abs_humidity`、`dew_point_c`、`vpd_kpa`、`battery_pct`、`rssi`  
條件：`>=` `>` `<=` `<` `==`

## 注意

- 目前桌機若只有 Xbox 接收器、**沒有 BLE**，HUD 開得了但讀不到即時值、也無法本機累積歷史。
- 需要 USB 藍牙或改在有 BLE 的機器上跑此程式。
