// Shared by the Edge Function and offline Node tests. No artwork bodies downloaded.
// Desktop SHA-256 verifies bytes; ETags bind HEAD checks to those verified objects.
export async function verifyArtwork(checks, headObject) {
  const failures = [];
  for (let start = 0; start < checks.length; start += 20) {
    const batch = await Promise.all(checks.slice(start, start + 20).map(async ({item, field, key}) => {
      const fail = reason => ({street_key: item.street_key, kind: field, reason});
      const size = item[field + '_size'];
      const sha = item[field + '_sha256'];
      const etag = item[field + '_etag'];
      // Three PNGs; the SVG remains local.
      if (!Number.isSafeInteger(size) || size < 1000 ||
          typeof sha !== 'string' || !/^[0-9a-f]{64}$/.test(sha) ||
          typeof etag !== 'string' || !/^[A-Za-z0-9_-]{8,128}$/.test(etag))
        return fail('Invalid expected artwork size, SHA-256 or ETag');
      try {
        const object = await headObject(key);
        if (object.ContentLength !== size)
          return fail('Artwork byte count differs from desktop verification');
        if ((object.ETag ?? '').replace(/^"|"$/g, '') !== etag)
          return fail('Artwork ETag differs from desktop verification');
        return null;
      } catch {
        return fail('Artwork object missing or could not be checked');
      }
    }));
    failures.push(...batch.filter(Boolean));
    if (failures.length) break;
  }
  return failures;
}
