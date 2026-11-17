-- =============================================================================
-- 재무제표 수치 저장용 PostgreSQL 스키마 (DART 사업보고서)
--
-- 실행:  psql -d <DB명> -f src/fin_research/storage/schema.sql
-- 여러 번 실행해도 안전하다(IF NOT EXISTS). 모든 객체는 `fin` 스키마 안에 만든다.
-- 대상: 연결재무제표 4종(재무상태표/포괄손익계산서/자본변동표/현금흐름표) + 연결 주석의 수치 표.
-- 데이터 적재 코드는 이 파일에 없다. 컬럼 설명은 docs/db_schema.md 참고.
-- 필요 버전: PostgreSQL 12 이상 (확장 모듈 불필요).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS fin;
SET search_path TO fin;

-- -----------------------------------------------------------------------------
-- 1. 기업
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS company (
    corp_code   char(8)     PRIMARY KEY,                -- DART 고유번호 (예: 01160363)
    stock_code  char(6),                                 -- 종목코드 (비상장이면 NULL)
    name        text        NOT NULL,                    -- 회사명
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT company_corp_code_fmt CHECK (corp_code ~ '^[0-9]{8}$'),
    CONSTRAINT company_stock_code_fmt CHECK (stock_code IS NULL OR stock_code ~ '^[0-9A-Z]{6}$')
);
CREATE UNIQUE INDEX IF NOT EXISTS company_stock_code_uq
    ON company (stock_code) WHERE stock_code IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 2. 보고서 (사업보고서 1건 = rcept_no 1개 = PDF 1개)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report (
    report_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rcept_no     char(14)    NOT NULL UNIQUE,            -- DART 접수번호 14자리
    corp_code    char(8)     NOT NULL REFERENCES company (corp_code),
    report_type  text        NOT NULL DEFAULT 'business', -- 사업보고서
    fiscal_year  smallint    NOT NULL,                    -- 사업연도 (예: 2025)
    rcept_date   date,                                    -- 접수(공시)일
    source_file  text,                                    -- 원문 PDF 파일명 (예: 20260318001622.pdf)
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT report_rcept_no_fmt CHECK (rcept_no ~ '^[0-9]{14}$')
);
CREATE INDEX IF NOT EXISTS report_corp_year_idx ON report (corp_code, fiscal_year);

-- -----------------------------------------------------------------------------
-- 3. 보고서의 기간 열 (당기 / 전기 / 전전기)
--    재무상태표는 기준일(period_end) 시점 값, 나머지는 period_start~period_end 기간 값.
--    같은 보고서의 모든 표가 이 3개 행을 공유한다.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_period (
    period_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id    bigint   NOT NULL REFERENCES report (report_id) ON DELETE CASCADE,
    role         text     NOT NULL,                       -- 열 역할
    term_no      smallint,                                -- 제N기 (예: 10)
    period_start date,                                    -- 회계기간 시작일 (재무상태표는 무시해도 됨)
    period_end   date     NOT NULL,                       -- 기준일 / 회계기간 종료일
    CONSTRAINT report_period_role_chk CHECK (role IN ('current', 'prior', 'prior2')),
    CONSTRAINT report_period_dates_chk CHECK (period_start IS NULL OR period_start <= period_end),
    CONSTRAINT report_period_uq UNIQUE (report_id, role)
);
CREATE INDEX IF NOT EXISTS report_period_end_idx ON report_period (period_end);

-- -----------------------------------------------------------------------------
-- 4. 재무제표 본문 (보고서 x 연결/별도 x 종류)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fin_statement (
    statement_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id        bigint   NOT NULL REFERENCES report (report_id) ON DELETE CASCADE,
    fs_scope         text     NOT NULL DEFAULT 'consolidated',
    statement_type   text     NOT NULL,
    title_raw        text,                                -- PDF에 적힌 표 제목 (예: 연결 포괄손익계산서)
    unit_label_raw   text,                                -- 원문 단위 표기 (예: (단위 : 원))
    unit_multiplier  integer  NOT NULL,                   -- 표시값 -> 원 환산 배수 (원=1, 천원=1000, 백만원=1000000)
    pdf_page_start   integer,                             -- 표가 시작하는 PDF 물리 쪽 번호 (1부터)
    pdf_page_end     integer,                             -- 표가 끝나는 쪽 (쪽을 넘어가는 표)
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fin_statement_scope_chk CHECK (fs_scope IN ('consolidated', 'separate')),
    -- BS 재무상태표 / IS 손익계산서 / CIS 포괄손익계산서 / SCE 자본변동표 / CF 현금흐름표
    -- 손익계산서와 포괄손익계산서가 하나로 합쳐진 회사는 CIS 하나만 저장한다.
    -- 둘이 따로 있는 회사는 IS와 CIS를 모두 저장할 수 있다.
    CONSTRAINT fin_statement_type_chk CHECK (statement_type IN ('BS', 'IS', 'CIS', 'SCE', 'CF')),
    CONSTRAINT fin_statement_unit_chk CHECK (unit_multiplier IN (1, 1000, 1000000)),
    CONSTRAINT fin_statement_pages_chk CHECK (pdf_page_end IS NULL OR pdf_page_start IS NULL
                                              OR pdf_page_end >= pdf_page_start),
    CONSTRAINT fin_statement_uq UNIQUE (report_id, fs_scope, statement_type)
);

-- -----------------------------------------------------------------------------
-- 5. 재무제표의 행 (계정과목)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fin_line_item (
    line_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    statement_id       bigint   NOT NULL REFERENCES fin_statement (statement_id) ON DELETE CASCADE,
    line_no            integer  NOT NULL,                 -- 표 안에서의 행 순서 (1부터, 쪽을 넘어도 이어서)
    account_name_raw   text     NOT NULL,                 -- PDF 원문 계정명 (주석 참조 번호 제외)
    account_name_norm  text,                              -- 공백/기호 정리한 이름 (조회·매칭용)
    std_account_code   text,                              -- 표준 계정코드 (예: IFRS/DART 코드). 나중에 채움, NULL 허용
    indent_level       smallint,                          -- 들여쓰기 깊이 (알 수 있을 때)
    is_total           boolean  NOT NULL DEFAULT false,   -- 합계/소계 행 여부
    note_refs          text,                              -- 원문 주석 참조 (예: '4,5,6,7')
    pdf_page           integer,                           -- 이 행이 실린 PDF 물리 쪽 번호
    chunk_id           text,                              -- 청크 id (예: 01160363_2025_consolidated_0048_01)
    CONSTRAINT fin_line_item_uq UNIQUE (statement_id, line_no)
);
CREATE INDEX IF NOT EXISTS fin_line_item_name_idx ON fin_line_item (account_name_norm);
CREATE INDEX IF NOT EXISTS fin_line_item_std_idx  ON fin_line_item (std_account_code)
    WHERE std_account_code IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 6. 재무제표 금액 (행 x 기간 [x 자본변동표 열])
--    - 자본변동표(SCE): 행=기초/당기순이익/기말 등, 열=자본금/이익잉여금 등 자본 구성요소.
--      period_id는 전기/당기 블록을 가리키고, 구성요소 열 이름은 column_key에 넣는다.
--    - 나머지 표는 column_key = '' (빈 문자열).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fin_value (
    value_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    line_id              bigint   NOT NULL REFERENCES fin_line_item (line_id) ON DELETE CASCADE,
    period_id            bigint   NOT NULL REFERENCES report_period (period_id),
    column_key           text     NOT NULL DEFAULT '',
    amount_raw           text,                            -- 원문 문자열 그대로 (예: '(1,234,567)', '-')
    amount_as_reported   bigint,                          -- 표시된 숫자 (괄호=음수 반영, 단위 환산 전)
    amount               bigint,                          -- 원 단위로 환산한 값 = amount_as_reported x unit_multiplier
    value_status         text     NOT NULL DEFAULT 'ok',
    pdf_page             integer  NOT NULL,               -- 값이 실린 PDF 물리 쪽 번호
    chunk_id             text,                            -- 값이 포함된 청크 id
    CONSTRAINT fin_value_status_chk CHECK (value_status IN ('ok', 'empty', 'dash')),
    -- ok이면 금액이 있고, empty(빈 칸)/dash('-' 표기)이면 금액은 NULL
    CONSTRAINT fin_value_amount_chk CHECK (
        (value_status = 'ok'  AND amount_as_reported IS NOT NULL AND amount IS NOT NULL)
     OR (value_status <> 'ok' AND amount_as_reported IS NULL     AND amount IS NULL)),
    CONSTRAINT fin_value_uq UNIQUE (line_id, period_id, column_key)
);
CREATE INDEX IF NOT EXISTS fin_value_period_idx ON fin_value (period_id);
CREATE INDEX IF NOT EXISTS fin_value_page_idx   ON fin_value (pdf_page);
CREATE INDEX IF NOT EXISTS fin_value_chunk_idx  ON fin_value (chunk_id) WHERE chunk_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 7. 주석 (보고서 x 주석 번호)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS note (
    note_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id       bigint   NOT NULL REFERENCES report (report_id) ON DELETE CASCADE,
    fs_scope        text     NOT NULL DEFAULT 'consolidated',
    note_no         text     NOT NULL,                    -- 주석 번호. '4', '4-1' 같은 하위 번호 허용
    title           text,                                 -- 주석 제목 (예: 매출채권 및 기타채권)
    pdf_page_start  integer,
    pdf_page_end    integer,
    CONSTRAINT note_scope_chk CHECK (fs_scope IN ('consolidated', 'separate')),
    CONSTRAINT note_pages_chk CHECK (pdf_page_end IS NULL OR pdf_page_start IS NULL
                                     OR pdf_page_end >= pdf_page_start),
    CONSTRAINT note_uq UNIQUE (report_id, fs_scope, note_no)
);

-- -----------------------------------------------------------------------------
-- 8. 주석 안의 수치 표 (주석 1개에 표가 여러 개일 수 있음)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS note_table (
    note_table_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    note_id          bigint   NOT NULL REFERENCES note (note_id) ON DELETE CASCADE,
    table_no         integer  NOT NULL,                   -- 주석 안에서의 표 순서 (1부터)
    caption          text,                                -- 표 제목/소제목 (예: 당기말, 전기말)
    unit_label_raw   text,
    unit_multiplier  integer  NOT NULL DEFAULT 1,
    pdf_page_start   integer,
    pdf_page_end     integer,
    chunk_id         text,                                -- 표가 시작하는 청크 id
    CONSTRAINT note_table_unit_chk CHECK (unit_multiplier IN (1, 1000, 1000000)),
    CONSTRAINT note_table_pages_chk CHECK (pdf_page_end IS NULL OR pdf_page_start IS NULL
                                           OR pdf_page_end >= pdf_page_start),
    CONSTRAINT note_table_uq UNIQUE (note_id, table_no)
);

-- -----------------------------------------------------------------------------
-- 9. 주석 표의 셀 (표 모양이 제각각이라 행/열 위치와 라벨로 저장)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS note_cell (
    cell_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    note_table_id        bigint   NOT NULL REFERENCES note_table (note_table_id) ON DELETE CASCADE,
    row_idx              integer  NOT NULL,               -- 0부터 (헤더 행 제외한 데이터 행 순서)
    col_idx              integer  NOT NULL,               -- 0부터 (라벨 열 제외한 값 열 순서)
    row_label            text     NOT NULL,               -- 행 이름 (예: 매출채권)
    col_label            text,                            -- 열 이름 (예: 장부금액, 당기말)
    period_id            bigint   REFERENCES report_period (period_id), -- 열이 당기/전기에 해당하면 연결
    text_raw             text,                            -- 셀 원문 (숫자가 아닌 값 포함)
    amount_raw           text,
    amount_as_reported   bigint,
    amount               bigint,                          -- 원 단위 환산값 (표 단위 반영)
    value_status         text     NOT NULL DEFAULT 'ok',
    pdf_page             integer  NOT NULL,
    chunk_id             text,
    CONSTRAINT note_cell_status_chk CHECK (value_status IN ('ok', 'empty', 'dash', 'text')),
    -- ok이면 금액이 있고, 그 외(empty/dash/text)이면 금액은 NULL
    CONSTRAINT note_cell_amount_chk CHECK (
        (value_status = 'ok'  AND amount_as_reported IS NOT NULL AND amount IS NOT NULL)
     OR (value_status <> 'ok' AND amount_as_reported IS NULL     AND amount IS NULL)),
    CONSTRAINT note_cell_uq UNIQUE (note_table_id, row_idx, col_idx)
);
CREATE INDEX IF NOT EXISTS note_cell_label_idx  ON note_cell (row_label);
CREATE INDEX IF NOT EXISTS note_cell_period_idx ON note_cell (period_id) WHERE period_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS note_cell_page_idx   ON note_cell (pdf_page);

-- -----------------------------------------------------------------------------
-- 9-1. 출처: 청크 원문 (값 -> chunk_id -> 쪽 번호/원문 텍스트)
--      chunks.json 의 청크를 그대로 저장한다. 값 테이블의 chunk_id 는 여기를 가리킨다.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_chunk (
    chunk_id    text        PRIMARY KEY,                  -- 예: 01160363_2025_consolidated_0048_01
    report_id   bigint      NOT NULL REFERENCES report (report_id) ON DELETE CASCADE,
    pdf_page    integer     NOT NULL,                     -- PDF 물리 쪽 번호
    page_id     text,                                     -- pages.json 의 page_id
    section     text,                                     -- 연결재무제표 / 연결재무제표 주석
    text        text        NOT NULL,                     -- 청크 원문 (약 600자)
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS source_chunk_report_page_idx ON source_chunk (report_id, pdf_page);

-- -----------------------------------------------------------------------------
-- 9-2. 표준 계정: 회사마다 다른 계정명을 하나의 코드로 묶어 회사 간 비교/비율 계산을 가능하게 한다.
--      std_account_alias 의 alias_norm 은 fin_line_item.account_name_norm(공백 제거) 과 같은 형태.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS std_account (
    std_account_code  text    PRIMARY KEY,               -- 예: ASSETS_TOTAL
    name_ko           text    NOT NULL,
    sort_order        integer NOT NULL
);

CREATE TABLE IF NOT EXISTS std_account_alias (
    statement_type    text NOT NULL,                      -- BS / IS / CIS / CF
    alias_norm        text NOT NULL,                      -- 공백 제거한 원문 계정명
    std_account_code  text NOT NULL REFERENCES std_account (std_account_code),
    PRIMARY KEY (statement_type, alias_norm),
    CONSTRAINT std_account_alias_type_chk CHECK (statement_type IN ('BS', 'IS', 'CIS', 'SCE', 'CF'))
);

INSERT INTO std_account (std_account_code, name_ko, sort_order) VALUES
    ('ASSETS_TOTAL',                  '자산총계',               10),
    ('ASSETS_CURRENT',                '유동자산',               11),
    ('ASSETS_NONCURRENT',             '비유동자산',             12),
    ('CASH_AND_EQUIVALENTS',          '현금및현금성자산',       13),
    ('LIABILITIES_TOTAL',             '부채총계',               20),
    ('LIABILITIES_CURRENT',           '유동부채',               21),
    ('LIABILITIES_NONCURRENT',        '비유동부채',             22),
    ('EQUITY_TOTAL',                  '자본총계',               30),
    ('LIABILITIES_AND_EQUITY_TOTAL',  '자본과부채총계',         31),
    ('REVENUE',                       '매출액',                 40),
    ('COST_OF_SALES',                 '매출원가',               41),
    ('GROSS_PROFIT',                  '매출총이익',             42),
    ('OPERATING_INCOME',              '영업이익',               43),
    ('PRETAX_INCOME',                 '법인세비용차감전순이익', 44),
    ('NET_INCOME',                    '당기순이익',             45),
    ('TOTAL_COMPREHENSIVE_INCOME',    '총포괄손익',             46),
    ('CASH_FLOW_OPERATING',           '영업활동현금흐름',       50),
    ('CASH_FLOW_INVESTING',           '투자활동현금흐름',       51),
    ('CASH_FLOW_FINANCING',           '재무활동현금흐름',       52),
    ('CASH_BEGIN',                    '기초현금및현금성자산',   53),
    ('CASH_END',                      '기말현금및현금성자산',   54)
ON CONFLICT (std_account_code) DO NOTHING;

-- 그룹: BS=재무상태표, PL=손익(IS/CIS 둘 다), CF=현금흐름표. 세 회사(에코프로비엠/에코프로/알테오젠)의 실제 계정명 기준.
INSERT INTO std_account_alias (statement_type, alias_norm, std_account_code)
SELECT t.st, v.alias, v.code
FROM (VALUES
    ('BS', '자산총계',                 'ASSETS_TOTAL'),
    ('BS', '유동자산',                 'ASSETS_CURRENT'),
    ('BS', '비유동자산',               'ASSETS_NONCURRENT'),
    ('BS', '현금및현금성자산',         'CASH_AND_EQUIVALENTS'),
    ('BS', '부채총계',                 'LIABILITIES_TOTAL'),
    ('BS', '유동부채',                 'LIABILITIES_CURRENT'),
    ('BS', '비유동부채',               'LIABILITIES_NONCURRENT'),
    ('BS', '자본총계',                 'EQUITY_TOTAL'),
    ('BS', '자본과부채총계',           'LIABILITIES_AND_EQUITY_TOTAL'),
    ('PL', '매출액',                   'REVENUE'),
    ('PL', '수익(매출액)',             'REVENUE'),
    ('PL', '영업수익',                 'REVENUE'),
    ('PL', '매출원가',                 'COST_OF_SALES'),
    ('PL', '매출총이익',               'GROSS_PROFIT'),
    ('PL', '매출총이익(손실)',         'GROSS_PROFIT'),
    ('PL', '영업이익',                 'OPERATING_INCOME'),
    ('PL', '영업이익(손실)',           'OPERATING_INCOME'),
    ('PL', '영업손익',                 'OPERATING_INCOME'),
    ('PL', '법인세비용차감전순이익',       'PRETAX_INCOME'),
    ('PL', '법인세비용차감전순이익(손실)', 'PRETAX_INCOME'),
    ('PL', '법인세비용차감전손익',         'PRETAX_INCOME'),
    ('PL', '법인세차감전순이익',           'PRETAX_INCOME'),
    ('PL', '법인세차감전순이익(손실)',     'PRETAX_INCOME'),
    ('PL', '당기순이익',               'NET_INCOME'),
    ('PL', '당기순이익(손실)',         'NET_INCOME'),
    ('PL', '당기순손익',               'NET_INCOME'),
    ('PL', '연결당기순이익',           'NET_INCOME'),
    ('PL', '총포괄손익',               'TOTAL_COMPREHENSIVE_INCOME'),
    ('PL', '당기총포괄손익',           'TOTAL_COMPREHENSIVE_INCOME'),
    ('PL', '총포괄이익',               'TOTAL_COMPREHENSIVE_INCOME'),
    ('PL', '총포괄이익(손실)',         'TOTAL_COMPREHENSIVE_INCOME'),
    ('CF', '영업활동현금흐름',         'CASH_FLOW_OPERATING'),
    ('CF', '영업활동으로인한현금흐름', 'CASH_FLOW_OPERATING'),
    ('CF', '투자활동현금흐름',         'CASH_FLOW_INVESTING'),
    ('CF', '투자활동으로인한현금흐름', 'CASH_FLOW_INVESTING'),
    ('CF', '재무활동현금흐름',         'CASH_FLOW_FINANCING'),
    ('CF', '재무활동으로인한현금흐름', 'CASH_FLOW_FINANCING'),
    ('CF', '기초현금및현금성자산',     'CASH_BEGIN'),
    ('CF', '기말현금및현금성자산',     'CASH_END')
) AS v(grp, alias, code)
JOIN (VALUES ('BS', 'BS'), ('PL', 'IS'), ('PL', 'CIS'), ('CF', 'CF')) AS t(grp, st) ON t.grp = v.grp
ON CONFLICT (statement_type, alias_norm) DO NOTHING;

-- 한 표 안에서 같은 표준코드는 한 행에만 붙는다 (적재 시 첫 번째 행에만 매핑)
CREATE UNIQUE INDEX IF NOT EXISTS fin_line_item_std_uq
    ON fin_line_item (statement_id, std_account_code) WHERE std_account_code IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 9-3. 외래키 추가 (이미 만든 DB에 이 파일을 다시 실행해도 안전하도록 없을 때만 추가)
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    fk record;
BEGIN
    FOR fk IN SELECT * FROM (VALUES
        ('fin_line_item', 'fin_line_item_chunk_fk', 'chunk_id',         'source_chunk', 'chunk_id'),
        ('fin_value',     'fin_value_chunk_fk',     'chunk_id',         'source_chunk', 'chunk_id'),
        ('note_table',    'note_table_chunk_fk',    'chunk_id',         'source_chunk', 'chunk_id'),
        ('note_cell',     'note_cell_chunk_fk',     'chunk_id',         'source_chunk', 'chunk_id'),
        ('fin_line_item', 'fin_line_item_std_fk',   'std_account_code', 'std_account',  'std_account_code')
    ) AS x(tbl, conname, col, ref_tbl, ref_col)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = fk.conname AND conrelid = ('fin.' || fk.tbl)::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE fin.%I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES fin.%I (%I)',
                fk.tbl, fk.conname, fk.col, fk.ref_tbl, fk.ref_col);
        END IF;
    END LOOP;
END
$$;

-- -----------------------------------------------------------------------------
-- 10. 조회용 뷰: 재무제표 금액을 회사/연도/기간과 함께 한 번에 본다
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_fin_value AS
SELECT c.corp_code, c.stock_code, c.name AS company_name,
       r.rcept_no, r.fiscal_year,
       s.fs_scope, s.statement_type,
       p.role AS period_role, p.term_no, p.period_start, p.period_end,
       l.line_no, l.account_name_raw, l.account_name_norm, l.std_account_code,
       v.column_key, v.amount_raw, v.amount_as_reported, v.amount,
       s.unit_multiplier, v.value_status, v.pdf_page, v.chunk_id
FROM fin_value v
JOIN fin_line_item l   ON l.line_id = v.line_id
JOIN fin_statement s   ON s.statement_id = l.statement_id
JOIN report r          ON r.report_id = s.report_id
JOIN company c         ON c.corp_code = r.corp_code
JOIN report_period p   ON p.period_id = v.period_id;

-- 표준 계정코드가 붙은 금액만 (회사 간 비교, 비율 계산의 기본 재료). 금액은 원 단위.
CREATE OR REPLACE VIEW v_fin_std_value AS
SELECT c.corp_code, c.name AS company_name, r.rcept_no, r.fiscal_year, s.fs_scope,
       p.role AS period_role, p.period_end,
       l.std_account_code, a.name_ko AS std_account_name,
       v.amount, v.pdf_page, v.chunk_id
FROM fin_value v
JOIN fin_line_item l  ON l.line_id = v.line_id
JOIN std_account a    ON a.std_account_code = l.std_account_code
JOIN fin_statement s  ON s.statement_id = l.statement_id
JOIN report r         ON r.report_id = s.report_id
JOIN company c        ON c.corp_code = r.corp_code
JOIN report_period p  ON p.period_id = v.period_id
WHERE v.value_status = 'ok' AND v.column_key = '';

-- 주요 비율 (%) : 회사 x 보고서 x 연결/별도 x 기간. 분모가 0이거나 값이 없으면 NULL.
CREATE OR REPLACE VIEW v_fin_ratio AS
WITH x AS (
    SELECT corp_code, company_name, rcept_no, fiscal_year, fs_scope, period_role, period_end,
           max(amount) FILTER (WHERE std_account_code = 'ASSETS_TOTAL')         AS assets_total,
           max(amount) FILTER (WHERE std_account_code = 'ASSETS_CURRENT')       AS assets_current,
           max(amount) FILTER (WHERE std_account_code = 'LIABILITIES_TOTAL')    AS liabilities_total,
           max(amount) FILTER (WHERE std_account_code = 'LIABILITIES_CURRENT')  AS liabilities_current,
           max(amount) FILTER (WHERE std_account_code = 'EQUITY_TOTAL')         AS equity_total,
           max(amount) FILTER (WHERE std_account_code = 'REVENUE')              AS revenue,
           max(amount) FILTER (WHERE std_account_code = 'GROSS_PROFIT')         AS gross_profit,
           max(amount) FILTER (WHERE std_account_code = 'OPERATING_INCOME')     AS operating_income,
           max(amount) FILTER (WHERE std_account_code = 'NET_INCOME')           AS net_income
    FROM v_fin_std_value
    GROUP BY corp_code, company_name, rcept_no, fiscal_year, fs_scope, period_role, period_end
)
SELECT corp_code, company_name, rcept_no, fiscal_year, fs_scope, period_role, period_end,
       round(liabilities_total * 100.0 / NULLIF(equity_total, 0), 2)        AS debt_ratio_pct,        -- 부채비율
       round(assets_current * 100.0 / NULLIF(liabilities_current, 0), 2)    AS current_ratio_pct,     -- 유동비율
       round(equity_total * 100.0 / NULLIF(assets_total, 0), 2)             AS equity_ratio_pct,      -- 자기자본비율
       round(gross_profit * 100.0 / NULLIF(revenue, 0), 2)                  AS gross_margin_pct,      -- 매출총이익률
       round(operating_income * 100.0 / NULLIF(revenue, 0), 2)              AS operating_margin_pct,  -- 영업이익률
       round(net_income * 100.0 / NULLIF(revenue, 0), 2)                    AS net_margin_pct         -- 순이익률
FROM x;

-- -----------------------------------------------------------------------------
-- 11. 테이블/컬럼 설명 (psql 의 \d+ 로 확인 가능)
-- -----------------------------------------------------------------------------
COMMENT ON SCHEMA fin IS '재무제표 수치 저장소 (DART 사업보고서 연결재무제표 + 연결 주석)';
COMMENT ON TABLE company       IS '기업 (corp_code 기준)';
COMMENT ON TABLE report        IS '사업보고서 1건 (접수번호 rcept_no 1개)';
COMMENT ON TABLE report_period IS '보고서의 당기/전기/전전기 기간 열 (제N기, 기준일)';
COMMENT ON TABLE fin_statement IS '재무제표 본문 (BS/IS/CIS/SCE/CF). 손익이 합쳐진 회사는 CIS만 존재';
COMMENT ON TABLE fin_line_item IS '재무제표의 행(계정과목). 원문 계정명 + 선택적 표준코드';
COMMENT ON TABLE fin_value     IS '재무제표 금액. 행 x 기간 [x 자본변동표 구성요소 열]';
COMMENT ON TABLE note          IS '주석 (번호 단위)';
COMMENT ON TABLE note_table    IS '주석 안의 수치 표';
COMMENT ON TABLE note_cell     IS '주석 표의 셀. 행/열 라벨과 위치로 저장';
COMMENT ON COLUMN fin_statement.unit_multiplier IS '표시값 -> 원 환산 배수. 원=1, 천원=1000, 백만원=1000000';
COMMENT ON COLUMN fin_value.amount_as_reported  IS 'PDF에 적힌 숫자. 괄호 표기는 음수로 변환, 단위 환산 전';
COMMENT ON COLUMN fin_value.amount              IS '원 단위 환산값 = amount_as_reported x unit_multiplier';
COMMENT ON COLUMN fin_value.value_status        IS 'ok=값 있음, empty=빈 칸, dash=''-'' 표기 (empty/dash는 amount NULL)';
COMMENT ON COLUMN fin_value.pdf_page            IS 'PDF 물리 쪽 번호 (pages.json의 pdf_page와 동일)';
COMMENT ON COLUMN fin_value.chunk_id            IS 'chunks.json의 chunk_id (FK 아님: 청크는 JSON/Chroma에 있음)';
COMMENT ON TABLE source_chunk      IS '출처: chunks.json 의 청크 원문. 값의 chunk_id 가 가리키는 곳 (쪽 번호, 섹션, 텍스트)';
COMMENT ON TABLE std_account       IS '표준 계정 코드 (회사 간 비교/비율 계산용)';
COMMENT ON TABLE std_account_alias IS '표준 계정으로 묶는 원문 계정명 매핑 (공백 제거한 이름 + 표 종류)';
COMMENT ON COLUMN fin_line_item.std_account_code IS '표준 계정코드. std_account_alias 로 적재 시 자동 매핑, 매핑 없으면 NULL';
COMMENT ON VIEW v_fin_std_value IS '표준 계정코드가 붙은 금액만 모은 뷰 (원 단위)';
COMMENT ON VIEW v_fin_ratio     IS '주요 비율(%): 부채비율, 유동비율, 자기자본비율, 매출총이익률, 영업이익률, 순이익률';
COMMENT ON VIEW v_fin_value IS '재무제표 금액 + 회사/보고서/기간/계정 정보를 합친 조회용 뷰';
