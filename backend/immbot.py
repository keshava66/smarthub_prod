"""IMMBOT tab - DEV_RAW.UNSTRUCTURED_EXPLORER.IMMBOT_FINAL (chunks + RIMDocs/trial metadata).
All the endpoints live in chunk_explorer.py; this file only describes the table."""
from chunk_explorer import build_chunk_router

DOC_COLUMNS = [
    "SOURCE", "DOC_ID", "FILE_PATH", "FILE_NAME", "FILE_HASH", "FILE_SIZE",
    "LAST_MODIFIED", "INGESTED_AT", "UPDATED_AT", "STATUS", "CHUNK_TOTAL", "PAGES_TOTAL",
    "OBJ_ID", "DOMAIN", "OBJECT_NAME", "R_OBJECT_TYPE", "A_STATUS",
    "R_VERSION_LABEL", "JJ_NUMBER", "JJ_TITLE", "DOCUMENT_TYPE", "ORIGINAL_FILE_NAME",
    "VEEVA_TYPE", "JJ_LEGACY_OBJ_TYPE", "JJ_SUBMISSION_TYPE",
    "ID_IN_SOURCE_SYSTEM", "CONTENT_LOCATION", "ADAPTOR_JOB_RUN_ID", "TITLE", "GROUP_NAME",
    "PRIMARY_GROUP", "ARTIFACT_NAME", "CD_MIGRATED_SOURCE", "DCTM_OBJ_LINK",
    "R_VERSION_LABEL_VIZ", "A_STATUS_VIZ", "OBJECT_NAME_VIZ", "R_OBJECT_TYPE_VIZ",
    "INSIGHT_FAMILY_CODE", "INSIGHT_FAMILY_NAME", "INSIGHT_THERAPEUTIC_AREA",
    "RIMDOCS_PRODUCT_CODE", "RIMDOCS_PRODUCT_DISPLAY_NAME", "FOR_EXCEPTIONS_LIST", "R_OBJECT_ID",
    "CLINICAL_TRIAL_ID", "PRIMARY_COMPOUND_ID", "GENERIC", "PROTOCOL_ID", "PHASE",
    "TA_TRIAL_INDICATION", "PROTOCOL_APPROVED_RIMDOCS", "TOM_HISTORICAL",
    "CODI_MDM", "CODI_VTMF", "PRIMARY_INDICATION",
]

FILTER_COLUMNS = [
    "INSIGHT_THERAPEUTIC_AREA", "GENERIC", "PHASE", "PRIMARY_INDICATION", "PROTOCOL_ID",
    "CODI_MDM", "CODI_VTMF", "TA_TRIAL_INDICATION", "DOMAIN", "A_STATUS", "STATUS",
    "ARTIFACT_NAME", "PRIMARY_GROUP", "R_VERSION_LABEL", "CD_MIGRATED_SOURCE",
    "PROTOCOL_APPROVED_RIMDOCS", "TOM_HISTORICAL", "R_OBJECT_TYPE", "JJ_LEGACY_OBJ_TYPE",
    "SOURCE", "DOCUMENT_TYPE", "VEEVA_TYPE", "JJ_SUBMISSION_TYPE",
    "CHUNK_LEVEL", "CONTENT_TYPE",
]

immbot_router, set_immbot_dependencies = build_chunk_router({
    "prefix": "/api/immbot",
    "tag": "IMMBOT",
    "cache_name": "immbot",
    "default_table": "DEV_RAW.UNSTRUCTURED_EXPLORER.IMMBOT_FINAL",
    "config_key": "immbot_table",
    "doc_columns": DOC_COLUMNS,
    "filter_columns": FILTER_COLUMNS,
    "search_columns": ["FILE_NAME", "OBJECT_NAME", "PROTOCOL_ID", "JJ_TITLE", "JJ_NUMBER", "SECTION_TITLE", "CHUNK_TEXT"],
    "distinct_counts": {"objects": "OBJ_ID", "domains": "DOMAIN", "protocols": "PROTOCOL_ID", "compounds": "GENERIC"},
    "extra_charts": {
        "compound-phase": lambda b: (
            f'SELECT "GENERIC", TO_VARCHAR("PHASE"), COUNT(DISTINCT "DOC_ID") FROM {b} GROUP BY 1, 2 ORDER BY 1'
        ),
        "trial-status": lambda b: (
            f'SELECT "CODI_MDM", COUNT(DISTINCT "DOC_ID") AS CNT FROM {b} GROUP BY 1 ORDER BY CNT DESC'
        ),
    },
    "default_dimension": "PRIMARY_INDICATION",
    "default_levels": ["INSIGHT_THERAPEUTIC_AREA", "GENERIC", "PROTOCOL_ID"],
})