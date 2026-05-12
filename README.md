# ZCode AI Agent

基于 Textual TUI 的 AI 编程助手，支持多端点自动后备与手动切换。

## 特性
- **多端点支持**: 在 `config.json` 中配置 `endpoints` 数组，主端点失败时自动尝试备用端点。
- **静默切换**: 自动切换过程不再弹窗干扰，仅在状态栏显示。
- **手动控制**: 通过 UI 上的 `EP` 按钮可即时循环切换当前使用的 API 端点。

## 配置说明 (config.json)
```json
{
  "api": {
    "endpoints": [
      { "base": "URL", "key": "KEY", "model": "MODEL" }
    ]
  }
}