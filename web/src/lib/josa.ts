type Pair = '은/는' | '이/가' | '을/를'

/** 낱말 끝 받침에 맞는 조사를 붙인다. 예: josa('나이', '은/는') → '나이는' */
export function josa(word: string, pair: Pair) {
  const [withBatchim, withoutBatchim] = pair.split('/')
  const code = word.charCodeAt(word.length - 1) - 0xac00
  // 한글이 아니면 받침이 없는 것으로 본다
  const hasBatchim = code >= 0 && code <= 11171 && code % 28 !== 0
  return word + (hasBatchim ? withBatchim : withoutBatchim)
}
