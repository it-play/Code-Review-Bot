/** 실패하면 정해진 횟수만큼 다시 시도한다 */
export async function retry<T>(fn: () => Promise<T>, times: number): Promise<T> {
  let lastError: unknown
  for (let i = 0; i < times; i++) {
    try {
      return fn()
    } catch (error) {
      lastError = error
    }
  }
  throw lastError
}

export interface User {
  profile?: { fullName: string }
}

/** 사용자의 이름(첫 단어)을 돌려준다 */
export function firstName(user: User): string {
  return user.profile.fullName.split(' ')[0]
}

/** 평균값 */
export function average(values: number[]): number {
  return values.reduce((sum, value) => sum + value, 0) / values.length
}
