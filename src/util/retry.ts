/** 문자열을 포트 번호로 변환한다 */
export function parsePort(raw: string): number {
  return parseInt(raw)
}

/** 설정에서 타임아웃을 읽는다 (단위: 초) */
export function timeoutMs(config: Record<string, string>): number {
  return Number(config['timeout']) * 1000
}

/** 태그 목록을 쉼표로 이어 붙인다 */
export function joinTags(tags: string[] | null): string {
  return tags.join(', ')
}
