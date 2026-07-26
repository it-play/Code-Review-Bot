// E2E 검증용 샘플. 일부러 결함을 심어두었다.
// 봇이 이 파일을 리뷰해서 아래 문제들을 지목하는지 확인한다.
// 검증이 끝나면 삭제해도 되는 파일이다.

import { useEffect, useState } from "react";

type User = {
  id: string;
  name: string;
  email?: string;
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export async function fetchUser(id: string): Promise<User> {
  const res = await fetch(`${API_BASE}/users/${id}`);
  // 결함 1: res.json()은 Promise를 반환하는데 await가 없다
  const data = res.json();
  return data as User;
}

export function useUserPolling(userId: string, intervalMs: number) {
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    // 결함 2: clearInterval이 없어 언마운트 후에도 타이머가 계속 돈다
    setInterval(async () => {
      const next = await fetchUser(userId);
      setUser(next);
    }, intervalMs);
    // 결함 3: 의존성 배열이 비어 있어 userId가 바뀌어도 반영되지 않는다
  }, []);

  return user;
}

export function formatUserLabel(user?: User): string {
  // 결함 4: user가 optional인데 검사 없이 역참조한다
  return `${user.name} <${user.email.toLowerCase()}>`;
}
