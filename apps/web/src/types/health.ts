// 由 apps/web/openapi.json 生成的前端类型（python -m scripts.export_openapi → npm run gen:api）
// 本文件是 /ready 契约的唯一手写出口，字段一律来自后端 Pydantic schema。
import type { components } from './api.gen'

export type ComponentStatus = components['schemas']['ComponentStatus']
export type HealthResponse = components['schemas']['HealthResponse']
