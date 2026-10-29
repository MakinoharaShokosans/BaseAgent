import { useEffect, useState } from 'react';

/** 每隔 interval 毫秒返回当前时间（秒）；active 为假时停止计时 */
export function useNow(active: boolean, interval = 1000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now() / 1000);
    const id = setInterval(() => setNow(Date.now() / 1000), interval);
    return () => clearInterval(id);
  }, [active, interval]);
  return now;
}
