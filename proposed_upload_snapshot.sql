-- PROPOSAL ONLY: not installed or executed by local development/tests.
-- Requires existing designs, design_assets, checkout_order_lines and
-- street_face_published_items tables from the catalogue schema.
-- A scalar JSON response is not subject to PostgREST's returned-row cap.
BEGIN;
CREATE OR REPLACE FUNCTION public.street_face_upload_snapshot_v1()
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ''
AS $$
DECLARE result jsonb;
BEGIN
  SELECT jsonb_build_object(
    'snapshot_version', 1,
    'designs', (SELECT coalesce(jsonb_agg(to_jsonb(d)), '[]'::jsonb) FROM
      (SELECT id,dataset_id,street_id,status FROM public.designs LIMIT 2001) d),
    'designs_count', (SELECT count(*) FROM public.designs),
    'design_assets', (SELECT coalesce(jsonb_agg(to_jsonb(a)), '[]'::jsonb) FROM
      (SELECT id,design_id,product_sku,version,front_mockup_key,rear_mockup_key,production_key
       FROM public.design_assets LIMIT 2001) a),
    'design_assets_count', (SELECT count(*) FROM public.design_assets),
    'checkout_order_lines', (SELECT count(*) FROM public.checkout_order_lines l
      JOIN public.designs d ON d.id=l.design_id WHERE d.dataset_id='street-face/hebden-bridge/v2'),
    'listed_keys', (SELECT coalesce(jsonb_agg(street_key), '[]'::jsonb)
      FROM (SELECT street_key FROM public.street_face_published_items WHERE dataset_id='street-face/hebden-bridge/v2' AND listed LIMIT 2001) listed_items),
    'listed_count', (SELECT count(*) FROM public.street_face_published_items
      WHERE dataset_id='street-face/hebden-bridge/v2' AND listed)
  ) INTO result;
  IF (result->>'designs_count')::bigint > 2000
     OR (result->>'design_assets_count')::bigint > 2000
     OR (result->>'listed_count')::bigint > 2000
     OR pg_catalog.octet_length(result::text) > 8388608 THEN
    RAISE EXCEPTION USING ERRCODE = '54000', MESSAGE = 'Ownership snapshot limit exceeded; uploads blocked';
  END IF;
  RETURN result;
END;
$$;
-- Deployment prerequisite: installer can assign postgres ownership and revoke every
-- existing direct grant. postgres must be the trusted administrative/BYPASSRLS
-- owner with schema USAGE and SELECT on all four tables. SECURITY DEFINER uses
-- that owner's permissions, not the caller's verified service_role permissions.
ALTER FUNCTION public.street_face_upload_snapshot_v1() OWNER TO postgres;
-- CREATE OR REPLACE preserves old ACLs: remove every historical named grantee.
DO $acl$
DECLARE grantee_name text;
BEGIN
  FOR grantee_name IN
    SELECT r.rolname FROM pg_catalog.pg_proc p
    CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) a
    JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
    WHERE p.oid = 'public.street_face_upload_snapshot_v1()'::pg_catalog.regprocedure
  LOOP
    EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION public.street_face_upload_snapshot_v1() FROM %I CASCADE', grantee_name);
  END LOOP;
END;
$acl$;
REVOKE ALL ON FUNCTION public.street_face_upload_snapshot_v1() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.street_face_upload_snapshot_v1() TO service_role, postgres;
-- Verify owner, definer context and direct ACL allowlist before committing.
DO $verify$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p
    JOIN pg_catalog.pg_roles r ON r.oid=p.proowner
    WHERE p.oid='public.street_face_upload_snapshot_v1()'::pg_catalog.regprocedure
      AND r.rolname='postgres' AND (r.rolsuper OR r.rolbypassrls)
      AND p.prosecdef AND p.proconfig @> ARRAY['search_path=""'])
    OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p
      CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) a
      LEFT JOIN pg_catalog.pg_roles r ON r.oid=a.grantee
      WHERE p.oid='public.street_face_upload_snapshot_v1()'::pg_catalog.regprocedure
        AND (r.rolname IS NULL OR r.rolname NOT IN ('postgres','service_role'))) THEN
    RAISE EXCEPTION 'Snapshot owner, execution context or ACL prerequisite failed';
  END IF;
  IF NOT pg_catalog.has_schema_privilege('postgres', 'public', 'USAGE')
    OR EXISTS (SELECT 1 FROM (VALUES ('public.designs'), ('public.design_assets'),
      ('public.street_face_published_items'), ('public.checkout_order_lines')) AS required(table_name)
      WHERE NOT pg_catalog.has_table_privilege('postgres', required.table_name, 'SELECT')) THEN
    RAISE EXCEPTION 'Snapshot owner requires schema USAGE and SELECT on all four tables';
  END IF;
END;
$verify$;
COMMIT;
