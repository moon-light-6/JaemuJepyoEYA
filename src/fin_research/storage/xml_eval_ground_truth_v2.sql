
CREATE SCHEMA IF NOT EXISTS fin;

CREATE TABLE fin.xml_eval_ground_truth_v2 (
    corp_code           varchar(8) NOT NULL,
    fiscal_year         integer NOT NULL,
    statement_scope     text NOT NULL,
    rcept_no            varchar(14) NOT NULL,

    question_id         text NOT NULL,
    dataset_version     text NOT NULL,
    question            text NOT NULL,
    answer_type         text NOT NULL,
    expected_value      jsonb NOT NULL,
    unit                text,
    category            text,

    sj_div              text,
    account_id          text,
    account_nm_raw      text,
    account_nm_norm     text,

    gold_sources        jsonb NOT NULL,
    candidate_sources   jsonb NOT NULL DEFAULT '[]'::jsonb,
    annotation_status   text NOT NULL,
    annotation_method   text,
    source_row          jsonb NOT NULL,
    loaded_at           timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        corp_code, fiscal_year, statement_scope,
        rcept_no, question_id
    ),

    CHECK (corp_code ~ '^[0-9]{8}$'),
    CHECK (rcept_no ~ '^[0-9]{14}$'),
    CHECK (statement_scope IN ('consolidated', 'separate')),
    CHECK (dataset_version IN ('v1', 'v2')),
    CHECK (answer_type IN ('numeric', 'comparison')),
    CHECK (annotation_status = 'verified'),
    CHECK (jsonb_typeof(expected_value) IN ('string', 'object')),
    CHECK (jsonb_typeof(gold_sources) = 'array')
);

CREATE INDEX xml_eval_gold_lookup_v2_idx
ON fin.xml_eval_ground_truth_v2 (
    corp_code, fiscal_year, statement_scope,
    rcept_no, account_nm_norm
);
