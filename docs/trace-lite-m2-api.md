# Trace-Lite M2 API

版本: `M2`

## 1) 查询推断调用片段

- `GET /api/v1/trace-lite/inferred`
- 参数:
- `time_window` 默认 `1 HOUR`
- `source_service` 可选
- `target_service` 可选
- `namespace` 可选
- `limit` 默认 `100`

返回字段（核心）:

- `fragment_id`
- `source_service`
- `target_service`
- `inference_method` (`request_id` / `time_window`)
- `confidence`
- `confidence_explain`
- `evidence_chain`

## 2) 老业务接入试点验收

- `GET /api/v1/trace-lite/pilot/readiness`
- 参数:
- `time_window` 默认 `24 HOUR`
- `min_services` 默认 `2`

返回字段（核心）:

- `ready`
- `inferred_service_count`
- `inferred_services`
- `sample_pairs`

## 3) 推断质量指标

- `GET /api/v1/quality/inference`
- 指标:
- `coverage`
- `inferred_ratio`
- `false_positive_rate`

## 4) 推断质量告警与抑制

- `GET /api/v1/quality/inference/alerts`
- `POST /api/v1/quality/inference/alerts/suppress?metric=<name>&enabled=true|false`
