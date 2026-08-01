/**
 * 签名预览授权(M9a 后端端点 REQ-PREVIEW-001 / SEC-PREVIEW-001)。
 *
 * 设计要点:
 *   - 预览产物由「不携带平台凭证」的独立 Origin 提供(生产 preview_origin; 本地开发降级同源);
 *   - 访问凭据是短期 HMAC 签名 URL, 过期后必须重新签发, 不得当永久字段缓存;
 *   - 前端只持有 token 与过期时间, 在过期前主动重签(见 PreviewPane.vue)。
 */

import { post } from './client'

export interface PreviewGrant {
  /** 绝对签名预览 URL(独立 Origin 或本地同源降级)。 */
  url: string
  artifact_id: number
  version: number
  /** 绝对过期时间戳(秒)。 */
  expires_at: number
  /** 有效期(秒)。 */
  expires_in: number
  /** 是否落在独立 Origin(true=物理隔离; false=本地同源降级)。 */
  isolated_origin: boolean
}

/** 请求预览签名授权。artifact_id 缺省时后端签发项目 head(最新可预览版本)。 */
export async function requestPreviewGrant(
  projectId: number,
  opts: { artifactId?: number | null; entry?: string } = {},
): Promise<PreviewGrant> {
  return post(`/api/projects/${projectId}/preview-grant`, {
    artifact_id: opts.artifactId ?? null,
    entry: opts.entry ?? 'index.html',
  })
}

/** 提前重签量(秒): 在过期前留足网络与渲染余量, 避免临界期 iframe 偶发 410。 */
export const PREVIEW_REFRESH_LEAD = 60
