--
-- PostgreSQL database dump
--

\restrict Tq4mYxwwrsDlFCMe9kkewrcH3plve3lMwN4aNHxklHdJYWKwFZibWC8nF73e3qb

-- Dumped from database version 17.10
-- Dumped by pg_dump version 17.10

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_logs (
    id integer NOT NULL,
    retailer_product_id integer NOT NULL,
    agent_name character varying NOT NULL,
    task_description character varying NOT NULL,
    input_data text,
    output_data text,
    execution_time double precision,
    status character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_logs_id_seq OWNED BY public.agent_logs.id;


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: chat_conversations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_conversations (
    id integer NOT NULL,
    retailer_id integer NOT NULL,
    product_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chat_conversations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chat_conversations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chat_conversations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chat_conversations_id_seq OWNED BY public.chat_conversations.id;


--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_messages (
    id integer NOT NULL,
    conversation_id integer NOT NULL,
    role character varying(16) NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chat_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chat_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chat_messages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chat_messages_id_seq OWNED BY public.chat_messages.id;


--
-- Name: competitor_price_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.competitor_price_history (
    id integer NOT NULL,
    competitor_product_id integer NOT NULL,
    price double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scraped_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: competitor_price_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.competitor_price_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: competitor_price_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.competitor_price_history_id_seq OWNED BY public.competitor_price_history.id;


--
-- Name: competitor_products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.competitor_products (
    id integer NOT NULL,
    catalog_product_id integer NOT NULL,
    platform_name character varying NOT NULL,
    product_name character varying NOT NULL,
    price double precision NOT NULL,
    rating double precision,
    product_url character varying,
    availability boolean,
    product_details text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    scraped_at timestamp with time zone DEFAULT now() NOT NULL,
    quantity_value double precision,
    quantity_unit character varying(16),
    pack_count integer,
    total_quantity double precision,
    total_quantity_unit character varying(16)
);


--
-- Name: competitor_products_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.competitor_products_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: competitor_products_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.competitor_products_id_seq OWNED BY public.competitor_products.id;


--
-- Name: price_analysis; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.price_analysis (
    id integer NOT NULL,
    retailer_product_id integer NOT NULL,
    average_market_price double precision,
    minimum_market_price double precision,
    maximum_market_price double precision,
    competitor_count integer,
    price_difference_percentage double precision,
    analysis_summary text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: price_analysis_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.price_analysis_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: price_analysis_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.price_analysis_id_seq OWNED BY public.price_analysis.id;


--
-- Name: product_catalog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.product_catalog (
    id integer NOT NULL,
    name character varying NOT NULL,
    category character varying,
    brand character varying,
    product_details text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_scraped_at timestamp with time zone,
    scraping_status character varying DEFAULT 'NEVER_SCRAPED'::character varying NOT NULL,
    marketplace_statuses json DEFAULT '{}'::json NOT NULL
);


--
-- Name: product_catalog_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.product_catalog_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: product_catalog_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.product_catalog_id_seq OWNED BY public.product_catalog.id;


--
-- Name: recommendations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recommendations (
    id integer NOT NULL,
    retailer_product_id integer NOT NULL,
    recommended_price double precision NOT NULL,
    expected_profit double precision NOT NULL,
    profit_percentage double precision NOT NULL,
    reasoning text NOT NULL,
    confidence_score double precision,
    accepted_by_user boolean,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    evidence_type character varying DEFAULT 'NO_EVIDENCE'::character varying NOT NULL,
    recommended_price_min double precision,
    recommended_price_max double precision
);


--
-- Name: recommendations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.recommendations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: recommendations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.recommendations_id_seq OWNED BY public.recommendations.id;


--
-- Name: retailer_products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.retailer_products (
    id integer NOT NULL,
    user_id integer NOT NULL,
    catalog_product_id integer NOT NULL,
    cost_price double precision NOT NULL,
    stock_quantity integer NOT NULL,
    minimum_profit_margin double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    name_override character varying,
    category_override character varying,
    brand_override character varying,
    product_details_override text,
    catalog_identity_pending boolean DEFAULT false NOT NULL,
    catalog_identity_staging boolean DEFAULT false NOT NULL,
    quantity_value double precision,
    quantity_unit character varying(16)
);


--
-- Name: retailer_products_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.retailer_products_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: retailer_products_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.retailer_products_id_seq OWNED BY public.retailer_products.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    retailer_name character varying NOT NULL,
    email character varying NOT NULL,
    password_hash character varying NOT NULL,
    business_name character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    reset_token character varying,
    reset_token_expiry timestamp with time zone
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: agent_logs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_logs ALTER COLUMN id SET DEFAULT nextval('public.agent_logs_id_seq'::regclass);


--
-- Name: chat_conversations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_conversations ALTER COLUMN id SET DEFAULT nextval('public.chat_conversations_id_seq'::regclass);


--
-- Name: chat_messages id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages ALTER COLUMN id SET DEFAULT nextval('public.chat_messages_id_seq'::regclass);


--
-- Name: competitor_price_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitor_price_history ALTER COLUMN id SET DEFAULT nextval('public.competitor_price_history_id_seq'::regclass);


--
-- Name: competitor_products id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitor_products ALTER COLUMN id SET DEFAULT nextval('public.competitor_products_id_seq'::regclass);


--
-- Name: price_analysis id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_analysis ALTER COLUMN id SET DEFAULT nextval('public.price_analysis_id_seq'::regclass);


--
-- Name: product_catalog id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_catalog ALTER COLUMN id SET DEFAULT nextval('public.product_catalog_id_seq'::regclass);


--
-- Name: recommendations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendations ALTER COLUMN id SET DEFAULT nextval('public.recommendations_id_seq'::regclass);


--
-- Name: retailer_products id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.retailer_products ALTER COLUMN id SET DEFAULT nextval('public.retailer_products_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: agent_logs agent_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_logs
    ADD CONSTRAINT agent_logs_pkey PRIMARY KEY (id);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: chat_conversations chat_conversations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_conversations
    ADD CONSTRAINT chat_conversations_pkey PRIMARY KEY (id);


--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);


--
-- Name: competitor_price_history competitor_price_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitor_price_history
    ADD CONSTRAINT competitor_price_history_pkey PRIMARY KEY (id);


--
-- Name: competitor_products competitor_products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitor_products
    ADD CONSTRAINT competitor_products_pkey PRIMARY KEY (id);


--
-- Name: price_analysis price_analysis_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_analysis
    ADD CONSTRAINT price_analysis_pkey PRIMARY KEY (id);


--
-- Name: product_catalog product_catalog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.product_catalog
    ADD CONSTRAINT product_catalog_pkey PRIMARY KEY (id);


--
-- Name: recommendations recommendations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendations
    ADD CONSTRAINT recommendations_pkey PRIMARY KEY (id);


--
-- Name: retailer_products retailer_products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.retailer_products
    ADD CONSTRAINT retailer_products_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: ix_agent_logs_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_logs_id ON public.agent_logs USING btree (id);


--
-- Name: ix_agent_logs_retailer_product_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_logs_retailer_product_id ON public.agent_logs USING btree (retailer_product_id);


--
-- Name: ix_chat_conversations_product_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_conversations_product_id ON public.chat_conversations USING btree (product_id);


--
-- Name: ix_chat_conversations_retailer_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_conversations_retailer_id ON public.chat_conversations USING btree (retailer_id);


--
-- Name: ix_chat_messages_conversation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_messages_conversation_id ON public.chat_messages USING btree (conversation_id);


--
-- Name: ix_competitor_price_history_competitor_product_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_competitor_price_history_competitor_product_id ON public.competitor_price_history USING btree (competitor_product_id);


--
-- Name: ix_competitor_price_history_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_competitor_price_history_id ON public.competitor_price_history USING btree (id);


--
-- Name: ix_competitor_products_catalog_product_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_competitor_products_catalog_product_id ON public.competitor_products USING btree (catalog_product_id);


--
-- Name: ix_competitor_products_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_competitor_products_id ON public.competitor_products USING btree (id);


--
-- Name: ix_competitor_products_platform_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_competitor_products_platform_name ON public.competitor_products USING btree (platform_name);


--
-- Name: ix_price_analysis_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_price_analysis_id ON public.price_analysis USING btree (id);


--
-- Name: ix_price_analysis_retailer_product_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_price_analysis_retailer_product_id ON public.price_analysis USING btree (retailer_product_id);


--
-- Name: ix_product_catalog_brand; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_product_catalog_brand ON public.product_catalog USING btree (brand);


--
-- Name: ix_product_catalog_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_product_catalog_category ON public.product_catalog USING btree (category);


--
-- Name: ix_product_catalog_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_product_catalog_id ON public.product_catalog USING btree (id);


--
-- Name: ix_product_catalog_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_product_catalog_name ON public.product_catalog USING btree (name);


--
-- Name: ix_recommendations_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recommendations_id ON public.recommendations USING btree (id);


--
-- Name: ix_recommendations_retailer_product_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recommendations_retailer_product_id ON public.recommendations USING btree (retailer_product_id);


--
-- Name: ix_retailer_products_catalog_product_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_retailer_products_catalog_product_id ON public.retailer_products USING btree (catalog_product_id);


--
-- Name: ix_retailer_products_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_retailer_products_id ON public.retailer_products USING btree (id);


--
-- Name: ix_retailer_products_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_retailer_products_user_id ON public.retailer_products USING btree (user_id);


--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);


--
-- Name: ix_users_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_users_id ON public.users USING btree (id);


--
-- Name: ix_users_reset_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_users_reset_token ON public.users USING btree (reset_token);


--
-- Name: agent_logs agent_logs_retailer_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_logs
    ADD CONSTRAINT agent_logs_retailer_product_id_fkey FOREIGN KEY (retailer_product_id) REFERENCES public.retailer_products(id) ON DELETE CASCADE;


--
-- Name: chat_conversations chat_conversations_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_conversations
    ADD CONSTRAINT chat_conversations_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.retailer_products(id) ON DELETE SET NULL;


--
-- Name: chat_conversations chat_conversations_retailer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_conversations
    ADD CONSTRAINT chat_conversations_retailer_id_fkey FOREIGN KEY (retailer_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: chat_messages chat_messages_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.chat_conversations(id) ON DELETE CASCADE;


--
-- Name: competitor_price_history competitor_price_history_competitor_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitor_price_history
    ADD CONSTRAINT competitor_price_history_competitor_product_id_fkey FOREIGN KEY (competitor_product_id) REFERENCES public.competitor_products(id) ON DELETE CASCADE;


--
-- Name: competitor_products competitor_products_catalog_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.competitor_products
    ADD CONSTRAINT competitor_products_catalog_product_id_fkey FOREIGN KEY (catalog_product_id) REFERENCES public.product_catalog(id) ON DELETE CASCADE;


--
-- Name: price_analysis price_analysis_retailer_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.price_analysis
    ADD CONSTRAINT price_analysis_retailer_product_id_fkey FOREIGN KEY (retailer_product_id) REFERENCES public.retailer_products(id) ON DELETE CASCADE;


--
-- Name: recommendations recommendations_retailer_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recommendations
    ADD CONSTRAINT recommendations_retailer_product_id_fkey FOREIGN KEY (retailer_product_id) REFERENCES public.retailer_products(id) ON DELETE CASCADE;


--
-- Name: retailer_products retailer_products_catalog_product_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.retailer_products
    ADD CONSTRAINT retailer_products_catalog_product_id_fkey FOREIGN KEY (catalog_product_id) REFERENCES public.product_catalog(id) ON DELETE CASCADE;


--
-- Name: retailer_products retailer_products_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.retailer_products
    ADD CONSTRAINT retailer_products_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict Tq4mYxwwrsDlFCMe9kkewrcH3plve3lMwN4aNHxklHdJYWKwFZibWC8nF73e3qb

\n