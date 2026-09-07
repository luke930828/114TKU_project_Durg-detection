# 多模態毒品交易防制系統 API 規格

## 1. AI 自動識別結果列表

### GET /api/ai-detections

用途：取得 AI 判讀後的案件資料列表。

### Response

```json
{
  "data": [
    {
      "id": 1,
      "time": "2024-12-15 14:30",
      "content": "疑似販售大麻相關對話及圖片",
      "drugType": "大麻",
      "language": "繁體中文",
      "riskLevel": "high",
      "score": 95,
      "caseNumber": "2025-121401",
      "detectedKeywords": ["420", "飛行", "燃料"],
      "aiAnalysis": [
        {
          "type": "黑話識別",
          "description": "出現毒品暗語",
          "confidence": 95
        }
      ],
      "riskFactors": ["使用黑話", "公開販售", "高風險毒品"]
    }
  ]
}