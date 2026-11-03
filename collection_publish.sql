-- Street Face v10: held-collection publication. Install once in Supabase SQL Editor.
-- Only service_role may commit. Never switches checkout or releases Prodigi fulfilment.
BEGIN;
CREATE TABLE IF NOT EXISTS public.street_face_published_items (
  dataset_id text NOT NULL,
  street_key text NOT NULL,
  design_id uuid NOT NULL REFERENCES public.designs(id),
  area_slug text NOT NULL,
  street_name text NOT NULL,
  postcode_district text NOT NULL DEFAULT '',
  geometry jsonb NOT NULL,
  listed boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (dataset_id, street_key)
);
CREATE TABLE IF NOT EXISTS public.street_face_publish_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_id text NOT NULL,
  operation_id uuid NOT NULL UNIQUE,
  mode text NOT NULL CHECK (mode IN ('complete','partial')),
  designs_count integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON public.street_face_published_items, public.street_face_publish_runs FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.street_face_published_items, public.street_face_publish_runs TO service_role;

-- The service-role-only RPC is a single database transaction: either every item
-- and catalogue availability change is committed, or none is.
CREATE OR REPLACE FUNCTION public.street_face_publish_held_v2(
  p_operation_id uuid,
  p_mode text,
  p_items jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE
  v_dataset constant text := 'street-face/hebden-bridge/v2';
  v_area constant text := 'hebden-bridge';
  v_count integer;
  v_done integer := 0;
  v_added integer := 0;
  v_updated integer := 0;
  v_key text;
  v_existing_id uuid;
  v_design_code text;
  v_asset public.design_assets%ROWTYPE;
  v_item jsonb;
  v_seq integer;
  v_existing jsonb;
BEGIN
  IF (SELECT auth.role()) IS DISTINCT FROM 'service_role' AND session_user <> 'postgres' THEN
    RAISE EXCEPTION 'service role only';
  END IF;
  IF p_mode NOT IN ('complete','partial') OR jsonb_typeof(p_items) <> 'array' THEN
    RAISE EXCEPTION 'invalid publish mode or items';
  END IF;
  v_count := jsonb_array_length(p_items);
  IF v_count < 1 OR v_count > 2000 THEN RAISE EXCEPTION 'invalid design count'; END IF;
  PERFORM pg_advisory_xact_lock(hashtext('street-face-release:' || v_dataset));
  SELECT to_jsonb(r) INTO v_existing FROM public.street_face_publish_runs r WHERE r.operation_id = p_operation_id;
  IF v_existing IS NOT NULL THEN
    RETURN jsonb_build_object('status','already_published','operation_id',p_operation_id,'run',v_existing);
  END IF;
  -- Refuse ANY publication if a real/checkout order line references this dataset,
  -- or any v2 design is active. This explicitly limits in-place R2 replacement
  -- to the no-customer-order, held-design phase of the project.
  IF EXISTS (
    SELECT 1 FROM public.checkout_order_lines l JOIN public.designs d ON d.id=l.design_id
    WHERE d.dataset_id=v_dataset
  ) THEN RAISE EXCEPTION 'v2 has checkout order lines; publication blocked'; END IF;
  IF EXISTS (SELECT 1 FROM public.designs WHERE dataset_id=v_dataset AND status <> 'hold') THEN
    RAISE EXCEPTION 'v2 contains non-held designs; publication blocked';
  END IF;
  IF (SELECT count(*) FROM (SELECT e->>'street_key' AS k FROM jsonb_array_elements(p_items) e GROUP BY 1) s) <> v_count THEN
    RAISE EXCEPTION 'duplicate street keys';
  END IF;
  FOR v_item IN SELECT value FROM jsonb_array_elements(p_items) LOOP
    v_key := v_item->>'street_key';
    IF v_key IS NULL OR length(v_key)<5 OR length(v_key)>400
       OR NULLIF(btrim(v_item->>'street_name'),'') IS NULL
       OR jsonb_typeof(v_item->'geometry') <> 'object'
       OR (v_item->'geometry'->>'type') NOT IN ('LineString','MultiLineString')
       OR coalesce(v_item->>'front_key','') NOT LIKE 'street-face/hebden-bridge/v2/mockups/%.png'
       OR coalesce(v_item->>'rear_key','') NOT LIKE 'street-face/hebden-bridge/v2/mockups/%.png'
       OR coalesce(v_item->>'production_key','') NOT LIKE 'street-face/hebden-bridge/v2/print_candidates/%.png'
       OR (v_item->>'front_size')::bigint < 1000
       OR (v_item->>'rear_size')::bigint < 1000
       OR (v_item->>'production_size')::bigint < 1000
       OR coalesce(v_item->>'front_etag','') !~ '^[A-Za-z0-9_-]{8,128}$'
       OR coalesce(v_item->>'rear_etag','') !~ '^[A-Za-z0-9_-]{8,128}$'
       OR coalesce(v_item->>'production_etag','') !~ '^[A-Za-z0-9_-]{8,128}$'
       OR (v_item->>'front_key') LIKE '%..%'
       OR (v_item->>'production_key') LIKE '%..%'
    THEN RAISE EXCEPTION 'invalid item %', left(coalesce(v_key,'<missing>'),100); END IF;

    SELECT id, design_code INTO v_existing_id, v_design_code
      FROM public.designs WHERE dataset_id=v_dataset AND street_id=v_key FOR UPDATE;
    IF v_existing_id IS NULL THEN
      -- New designs get permanent codes independent of artwork filenames.
      SELECT coalesce(max(substring(design_code from '^HBV2-([0-9]{4})$')::integer),4999)+1
        INTO v_seq FROM public.designs WHERE design_code ~ '^HBV2-[0-9]{4}$'
        AND substring(design_code from '^HBV2-([0-9]{4})$')::integer >= 5000;
      IF v_seq > 9999 THEN RAISE EXCEPTION 'v2 design code range exhausted'; END IF;
      v_design_code := 'HBV2-' || lpad(v_seq::text,4,'0');
      INSERT INTO public.designs(design_code,dataset_id,street_id,street_name,locality,status)
      VALUES (v_design_code,v_dataset,v_key,v_item->>'street_name','Hebden Bridge','hold')
      RETURNING id INTO v_existing_id;
      v_added := v_added+1;
    ELSE
      UPDATE public.designs SET street_name=v_item->>'street_name',updated_at=now()
      WHERE id=v_existing_id AND status='hold';
      v_updated := v_updated+1;
    END IF;
    SELECT * INTO v_asset FROM public.design_assets
      WHERE design_id=v_existing_id AND product_sku='GLOBAL-MUG-W' AND version=1 FOR UPDATE;
    IF FOUND THEN
      IF v_asset.validation_status <> 'pending' THEN RAISE EXCEPTION 'non-pending asset %',v_design_code; END IF;
      UPDATE public.design_assets SET
        front_mockup_key=v_item->>'front_key',rear_mockup_key=v_item->>'rear_key',
        production_key=v_item->>'production_key',
        front_size=(v_item->>'front_size')::bigint,
        rear_size=(v_item->>'rear_size')::bigint,
        production_size=(v_item->>'production_size')::bigint,
        front_etag=v_item->>'front_etag',rear_etag=v_item->>'rear_etag',
        production_etag=v_item->>'production_etag',updated_at=now()
      WHERE id=v_asset.id;
    ELSE
      INSERT INTO public.design_assets(design_id,product_sku,version,
        front_mockup_key,rear_mockup_key,production_key,
        front_size,rear_size,production_size,front_etag,rear_etag,production_etag,validation_status)
      VALUES(v_existing_id,'GLOBAL-MUG-W',1,
        v_item->>'front_key',v_item->>'rear_key',v_item->>'production_key',
        (v_item->>'front_size')::bigint,(v_item->>'rear_size')::bigint,(v_item->>'production_size')::bigint,
        v_item->>'front_etag',v_item->>'rear_etag',v_item->>'production_etag','pending');
    END IF;
    INSERT INTO public.street_face_published_items(dataset_id,street_key,design_id,area_slug,street_name,postcode_district,geometry,listed)
    VALUES(v_dataset,v_key,v_existing_id,v_area,v_item->>'street_name',coalesce(v_item->>'postcode_district',''),v_item->'geometry',true)
    ON CONFLICT(dataset_id,street_key) DO UPDATE SET
      design_id=excluded.design_id,street_name=excluded.street_name,
      postcode_district=excluded.postcode_district,geometry=excluded.geometry,
      listed=true,updated_at=now();
    v_done := v_done+1;
  END LOOP;
  IF p_mode='complete' THEN
    UPDATE public.street_face_published_items SET listed=false,updated_at=now()
      WHERE dataset_id=v_dataset AND street_key NOT IN (
        SELECT e->>'street_key' FROM jsonb_array_elements(p_items) e
      );
  END IF;
  INSERT INTO public.street_face_publish_runs(dataset_id,operation_id,mode,designs_count)
    VALUES(v_dataset,p_operation_id,p_mode,v_done);
  RETURN jsonb_build_object('status','published','mode',p_mode,'processed',v_done,
    'new_designs',v_added,'existing_designs',v_updated,
    'listed', (SELECT count(*) FROM public.street_face_published_items WHERE dataset_id=v_dataset AND listed),
    'checkout_enabled',false,'prodigi_enabled',false);
END;
$$;
REVOKE ALL ON FUNCTION public.street_face_publish_held_v2(uuid,text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.street_face_publish_held_v2(uuid,text,jsonb) TO service_role;

-- Public read-only RPC: safe storefront mockup references & street geometries only.
CREATE OR REPLACE FUNCTION public.street_face_current_v2()
RETURNS TABLE (
 design_code text, street_id text, street_name text, postcode_district text,
 geometry jsonb, front_mockup_key text, rear_mockup_key text,
 front_etag text, rear_etag text
) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=''
AS $$
 SELECT d.design_code,i.street_key,i.street_name,i.postcode_district,i.geometry,
   a.front_mockup_key,a.rear_mockup_key,a.front_etag,a.rear_etag
 FROM public.street_face_published_items i
 JOIN public.designs d ON d.id=i.design_id
 JOIN public.design_assets a ON a.design_id=d.id AND a.product_sku='GLOBAL-MUG-W' AND a.version=1
 WHERE i.dataset_id='street-face/hebden-bridge/v2'
   AND i.listed AND d.status='hold' AND a.validation_status='pending'
 ORDER BY d.design_code
$$;
REVOKE ALL ON FUNCTION public.street_face_current_v2() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.street_face_current_v2() TO anon,authenticated,service_role;
COMMIT;
