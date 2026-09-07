# https://newpanjing.github.io/simpleui_docs/config.html#%E5%9B%BE%E6%A0%87%E8%AF%B4%E6%98%8E

from pathlib import Path
from decouple import config
import os
import secrets

BASE_DIR = Path(__file__).resolve().parent.parent


def parse_csv(value):
    return [item.strip() for item in value.split(',') if item.strip()]

# 安全修复：DEBUG 模式默认关闭，生产环境不应开启 DEBUG
# 开发环境请通过 .env 文件设置 DEBUG=True
DEBUG = config('DEBUG', default=False, cast=bool)


def load_secret_key():
    secret_key = config('SECRET_KEY', default='').strip()
    if secret_key:
        return secret_key

    secret_key_file = Path(config('SECRET_KEY_FILE', default=str(BASE_DIR / '.secret_key')))

    try:
        if secret_key_file.exists():
            return secret_key_file.read_text(encoding='utf-8').strip()

        generated_secret_key = secrets.token_urlsafe(50)
        secret_key_file.write_text(f'{generated_secret_key}\n', encoding='utf-8')
        return generated_secret_key
    except OSError as exc:
        if DEBUG:
            return 'django-insecure-runnergo-testhub-dev-only-secret-key'

        raise ValueError(
            "安全错误：SECRET_KEY 未设置，且无法写入 SECRET_KEY_FILE！\n"
            "请在 .env 文件或环境变量中设置 SECRET_KEY，或配置可写的 SECRET_KEY_FILE。\n"
            "生成安全密钥的方法：python -c \"import secrets; print(secrets.token_urlsafe(50))\""
        ) from exc


SECRET_KEY = load_secret_key()

# 用于飞书/企微/邮件中的外部可访问链接；本地部署统一使用 localhost，避免网络切换导致旧 IP 失效。
APP_PUBLIC_BASE_URL = config('APP_PUBLIC_BASE_URL', default='http://localhost:8000').rstrip('/')
LOCAL_TRUSTED_MODE = config('LOCAL_TRUSTED_MODE', default=False, cast=bool)
RUNNERGO_MANAGEMENT_API_URL = config(
    'RUNNERGO_MANAGEMENT_API_URL',
    default='http://manage:30000/management/api/v1',
).rstrip('/')
TEST_DATA_CENTER_AGENT_TOKEN = config(
    'TEST_DATA_CENTER_AGENT_TOKEN',
    default='runnergo-local-agent-token',
).strip()
# RunnerGo 管理域 MySQL（third_notice 等）。manage 未提供 internal 通知接口时，
# 用于回退直连读取第三方集成的 Webhook 机器人配置。
RUNNERGO_MYSQL_HOST = config('RUNNERGO_MYSQL_HOST', default='').strip()
RUNNERGO_MYSQL_PORT = config('RUNNERGO_MYSQL_PORT', default='3306')
RUNNERGO_MYSQL_USER = config('RUNNERGO_MYSQL_USER', default='root').strip()
RUNNERGO_MYSQL_PASSWORD = config('RUNNERGO_MYSQL_PASSWORD', default='')
RUNNERGO_MYSQL_DATABASE = config('RUNNERGO_MYSQL_DATABASE', default='runnergo').strip()
PROMETHEUS_TARGET_DIR = config(
    'PROMETHEUS_TARGET_DIR',
    default=str(BASE_DIR.parent / 'deploy' / 'prometheus' / 'targets'),
)
PROMETHEUS_RELOAD_URL = config('PROMETHEUS_RELOAD_URL', default='http://localhost:9090/-/reload')
PERFORMANCE_EXPORTER_RESTART_ENABLED = config(
    'PERFORMANCE_EXPORTER_RESTART_ENABLED', default=False, cast=bool
)

# 根据DEBUG模式设置ALLOWED_HOSTS，生产环境不应使用通配符
if DEBUG:
    ALLOWED_HOSTS = ['*']
else:
    ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1',
                           cast=parse_csv)

DJANGO_APPS = [
    'simpleui',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework.authtoken',
    'rest_framework_simplejwt',  # 添加JWT支持
    'rest_framework_simplejwt.token_blacklist',  # JWT token黑名单
    'corsheaders',
    'django_filters',
    'drf_spectacular',
    'channels',
]

ANALYTICS_ENABLED = config('ANALYTICS_ENABLED', default=False, cast=bool)
ANALYTICS_MAX_BATCH_SIZE = config('ANALYTICS_MAX_BATCH_SIZE', default=20, cast=int)
REGISTRATION_STATS_ENABLED = config('REGISTRATION_STATS_ENABLED', default=False, cast=bool)
REGISTRATION_STATS_VISIBLE_USERNAMES = config(
    'REGISTRATION_STATS_VISIBLE_USERNAMES',
    default='',
    cast=parse_csv,
)
APP_USE_HTTPS = config('APP_USE_HTTPS', default=not DEBUG, cast=bool)
TRUST_PROXY_SSL_HEADER = config('TRUST_PROXY_SSL_HEADER', default=APP_USE_HTTPS, cast=bool)

# AI 模型与 Dify 等可配置外呼地址默认只允许公网目标。企业内网模型必须
# 显式配置域名/CIDR 白名单，避免连接测试和生成接口成为 SSRF 代理。
AI_OUTBOUND_ALLOW_PRIVATE_URLS = config(
    'AI_OUTBOUND_ALLOW_PRIVATE_URLS', default=False, cast=bool
)
AI_OUTBOUND_ALLOWED_HOSTS = config(
    'AI_OUTBOUND_ALLOWED_HOSTS', default='', cast=parse_csv
)
AI_OUTBOUND_ALLOWED_CIDRS = config(
    'AI_OUTBOUND_ALLOWED_CIDRS', default='', cast=parse_csv
)
SKILL_RUNNER_URL = config(
    'SKILL_RUNNER_URL', default='http://testhub-skill-runner:8080'
).strip()
SKILL_RUNNER_TOKEN = config('SKILL_RUNNER_TOKEN', default='').strip()
SKILL_RUNNER_REQUEST_TIMEOUT_SECONDS = config(
    'SKILL_RUNNER_REQUEST_TIMEOUT_SECONDS', default=915, cast=int
)
SKILL_RUNNER_MAX_EXECUTION_SECONDS = config(
    'SKILL_RUNNER_MAX_EXECUTION_SECONDS', default=900, cast=int
)
E2E_BROWSER_CHANNEL = config('E2E_BROWSER_CHANNEL', default='').strip()
E2E_BROWSER_FALLBACK_CHANNEL = config('E2E_BROWSER_FALLBACK_CHANNEL', default='chrome').strip()
E2E_BROWSER_EXECUTABLE_PATH = config('E2E_BROWSER_EXECUTABLE_PATH', default='').strip()
E2E_ACTION_TIMEOUT_MS = max(100, min(config('E2E_ACTION_TIMEOUT_MS', default=15000, cast=int), 120000))
E2E_NAVIGATION_TIMEOUT_MS = max(
    100,
    min(config('E2E_NAVIGATION_TIMEOUT_MS', default=30000, cast=int), 120000),
)
E2E_PLANNER_TIMEOUT_SECONDS = max(
    10,
    min(config('E2E_PLANNER_TIMEOUT_SECONDS', default=90, cast=int), 300),
)
E2E_PAGE_INSPECTION_TIMEOUT_MS = max(
    1000,
    min(config('E2E_PAGE_INSPECTION_TIMEOUT_MS', default=10000, cast=int), 30000),
)
E2E_IGNORE_HTTPS_ERRORS = config('E2E_IGNORE_HTTPS_ERRORS', default=False, cast=bool)
E2E_IOS_APPIUM_SERVER_URL = config('E2E_IOS_APPIUM_SERVER_URL', default='').strip()
APPLITOOLS_API_KEY = config('APPLITOOLS_API_KEY', default='').strip()
APPLITOOLS_SERVER_URL = config('APPLITOOLS_SERVER_URL', default='').strip()
APPLITOOLS_APP_NAME = config('APPLITOOLS_APP_NAME', default='RunnerGo AI E2E').strip()
APPLITOOLS_BATCH_NAME = config('APPLITOOLS_BATCH_NAME', default='RunnerGo E2E').strip()
E2E_APPIUM_ALLOWED_HOSTS = config(
    'E2E_APPIUM_ALLOWED_HOSTS',
    default='127.0.0.1,localhost,host.docker.internal,testhub-backend',
    cast=parse_csv,
)
SECURITY_CREDENTIALS_KEY = config('SECURITY_CREDENTIALS_KEY', default=SECRET_KEY)
MODEL_PROVIDER = config('MODEL_PROVIDER', default='openai-compatible')
BASE_URL = config('BASE_URL', default='')
API_KEY = config('API_KEY', default='')
MODEL_NAME = config('MODEL_NAME', default='')
LLM_REQUEST_RETRY_COUNT = max(0, min(config('LLM_REQUEST_RETRY_COUNT', default=2, cast=int), 5))

LOCAL_APPS = [
    'apps.users',
    'apps.projects',
    'apps.testcases',
    'apps.testsuites',
    'apps.executions',
    'apps.reports',
    'apps.reviews',
    'apps.versions',
    'apps.assistant',
    'apps.requirement_analysis',
    'apps.app_automation.apps.AppAutomationConfig',  # APP自动化测试
    'apps.agent.apps.AgentConfig',
    'apps.core',
    'apps.data_factory',
    'apps.test_assets',
    'apps.performance_rule',
    'apps.sql_console',
]

if ANALYTICS_ENABLED or REGISTRATION_STATS_ENABLED:
    LOCAL_APPS.append('apps.analytics')

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    # 安全修复：移除 DisableCSRFMiddleware，不再全局禁用 CSRF 保护
    # 对于 JWT 认证的 API，CSRF 保护不影响（JWT 不依赖 cookie）
    # 对于 Session 认证的 API，CSRF 保护是必需的
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'backend.wsgi.application'
ASGI_APPLICATION = 'backend.asgi.application'

# 支持 SQLite 和 MySQL 切换，通过 DB_ENGINE 环境变量控制
_db_engine = config('DB_ENGINE', default='mysql')
if _db_engine == 'sqlite':
    _sqlite_database_path = Path(
        config('SQLITE_DATABASE_PATH', default=str(BASE_DIR / 'db.sqlite3'))
    )
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': _sqlite_database_path,
            'OPTIONS': {
                # Web requests and Celery workers share this local database.
                # Give short-lived writers time to finish instead of failing
                # immediately with ``database is locked``.
                'timeout': max(5, min(config('SQLITE_BUSY_TIMEOUT_SECONDS', default=30, cast=int), 120)),
            },
            'TEST': {
                'NAME': ':memory:',
            },
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': config('DB_NAME', default='test'),
            'USER': config('DB_USER', default='root'),
            # 安全修复：数据库密码不再硬编码默认值，必须通过环境变量设置
            'PASSWORD': config('DB_PASSWORD', default=''),
            'HOST': config('DB_HOST', default='localhost'),
            'PORT': config('DB_PORT', default='3306'),
            'OPTIONS': {
                'charset': 'utf8mb4',
                'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }

    # 安全检查：生产环境必须设置数据库密码
    if not DEBUG and not config('DB_PASSWORD', default=''):
        raise ValueError(
            "安全错误：生产环境必须设置 DB_PASSWORD 环境变量！\n"
            "请在 .env 文件中配置数据库密码。"
        )

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/
# Supported language codes: 'en-us' (English), 'zh-hans' (Simplified Chinese), 'ja' (Japanese), 'ko' (Korean), etc.
# See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones for timezone list
LANGUAGE_CODE = config('LANGUAGE_CODE', default='zh-hans')
TIME_ZONE = config('TIME_ZONE', default='Asia/Shanghai')
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'static_files')

# 数据工厂的静态文件目录
STATIC_FILES_URL = '/static_files/'
STATIC_FILES_ROOT = os.path.join(BASE_DIR, 'static_files')

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom User Model
AUTH_USER_MODEL = 'users.User'

# DRF Settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.users.authentication.LocalTrustedAuthentication',
        'rest_framework_simplejwt.authentication.JWTAuthentication',  # JWT认证（优先）
        'rest_framework.authentication.TokenAuthentication',  # 保留Token认证（兼容）
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': config('API_ANON_RATE', default='60/min'),
        'user': config('API_USER_RATE', default='1200/hour'),
    },
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}

from datetime import timedelta

# RunnerGo 企业部署统一采用长期有效期。这里的长期定义为 3650 天（10 年）；
# 任何可配置值都不能把平台凭证重新降级为分钟、小时或短期天数。
LONG_TERM_VALIDITY_DAYS = 3650
LONG_TERM_VALIDITY_SECONDS = LONG_TERM_VALIDITY_DAYS * 24 * 60 * 60

TESTHUB_ACCESS_TOKEN_LIFETIME_DAYS = config(
    'TESTHUB_ACCESS_TOKEN_LIFETIME_DAYS', default=LONG_TERM_VALIDITY_DAYS, cast=int
)
TESTHUB_REFRESH_TOKEN_LIFETIME_DAYS = config(
    'TESTHUB_REFRESH_TOKEN_LIFETIME_DAYS', default=LONG_TERM_VALIDITY_DAYS, cast=int
)
TESTHUB_ACCESS_TOKEN_LIFETIME_DAYS = max(
    LONG_TERM_VALIDITY_DAYS, TESTHUB_ACCESS_TOKEN_LIFETIME_DAYS
)
TESTHUB_REFRESH_TOKEN_LIFETIME_DAYS = max(
    LONG_TERM_VALIDITY_DAYS, TESTHUB_REFRESH_TOKEN_LIFETIME_DAYS
)
AI_SSE_TOKEN_MAX_AGE_SECONDS = max(
    LONG_TERM_VALIDITY_SECONDS,
    config('AI_SSE_TOKEN_MAX_AGE_SECONDS', default=LONG_TERM_VALIDITY_SECONDS, cast=int),
)

# JWT Settings
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=TESTHUB_ACCESS_TOKEN_LIFETIME_DAYS),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=TESTHUB_REFRESH_TOKEN_LIFETIME_DAYS),
    'ROTATE_REFRESH_TOKENS': True,  # 刷新时轮换refresh_token
    'BLACKLIST_AFTER_ROTATION': True,  # 旧的refresh_token加入黑名单
    'UPDATE_LAST_LOGIN': True,  # 更新最后登录时间

    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'VERIFYING_KEY': None,
    'AUDIENCE': None,
    'ISSUER': None,
    'JWK_URL': None,
    'LEEWAY': 0,

    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'USER_AUTHENTICATION_RULE': 'rest_framework_simplejwt.authentication.default_user_authentication_rule',

    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
    'TOKEN_TYPE_CLAIM': 'token_type',
    'TOKEN_USER_CLASS': 'rest_framework_simplejwt.models.TokenUser',

    'JTI_CLAIM': 'jti',

    'SLIDING_TOKEN_REFRESH_EXP_CLAIM': 'refresh_exp',
    'SLIDING_TOKEN_LIFETIME': timedelta(days=LONG_TERM_VALIDITY_DAYS),
    'SLIDING_TOKEN_REFRESH_LIFETIME': timedelta(days=LONG_TERM_VALIDITY_DAYS),
}

# CSRF / Session Settings - 支持通过 .env 控制 HTTP/HTTPS
CSRF_USE_SESSIONS = config('CSRF_USE_SESSIONS', default=False, cast=bool)
CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=APP_USE_HTTPS, cast=bool)
SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=APP_USE_HTTPS, cast=bool)
CSRF_COOKIE_HTTPONLY = config('CSRF_COOKIE_HTTPONLY', default=not DEBUG, cast=bool)
CSRF_COOKIE_SAMESITE = config('CSRF_COOKIE_SAMESITE', default='Lax')
SESSION_COOKIE_SAMESITE = config('SESSION_COOKIE_SAMESITE', default='Lax')

# 生产安全响应策略；本地 HTTP 调试可通过环境变量显式关闭。
SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=APP_USE_HTTPS, cast=bool)
SECURE_HSTS_SECONDS = config(
    'SECURE_HSTS_SECONDS', default=31536000 if APP_USE_HTTPS else 0, cast=int
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config(
    'SECURE_HSTS_INCLUDE_SUBDOMAINS', default=APP_USE_HTTPS, cast=bool
)
SECURE_HSTS_PRELOAD = config('SECURE_HSTS_PRELOAD', default=APP_USE_HTTPS, cast=bool)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = config('SECURE_REFERRER_POLICY', default='same-origin')
EXPOSE_API_DOCS = config('EXPOSE_API_DOCS', default=DEBUG, cast=bool)

PUBLIC_REPORT_MAX_AGE_SECONDS = max(
    LONG_TERM_VALIDITY_SECONDS,
    config(
        'PUBLIC_REPORT_MAX_AGE_SECONDS',
        default=LONG_TERM_VALIDITY_SECONDS,
        cast=int,
    ),
)
PUBLIC_TEMPLATE_MAX_AGE_SECONDS = max(
    LONG_TERM_VALIDITY_SECONDS,
    config(
        'PUBLIC_TEMPLATE_MAX_AGE_SECONDS',
        default=LONG_TERM_VALIDITY_SECONDS,
        cast=int,
    ),
)

if TRUST_PROXY_SSL_HEADER:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# CORS Settings
cors_origins_str = config('CORS_ALLOWED_ORIGINS', default='')
parsed_cors_origins = parse_csv(cors_origins_str)

if DEBUG:
    # 开发环境默认允许本地地址，同时合并环境变量里的配置
    # 优先使用环境变量配置的地址，确保服务器IP优先级最高
    CORS_ALLOWED_ORIGINS = [
        *parsed_cors_origins,  # 环境变量配置的地址优先
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ]
    CORS_ALLOW_CREDENTIALS = True
    # 支持EventSource (SSE) 的额外CORS头部
    CORS_ALLOW_HEADERS = [
        'accept',
        'accept-encoding',
        'authorization',
        'content-type',
        'dnt',
        'origin',
        'user-agent',
        'x-csrftoken',
        'x-requested-with',
        'cache-control',  # 添加 SSE 需要的头部
    ]
else:
    # 安全修复：生产环境必须配置 CORS_ALLOWED_ORIGINS
    # CORS_ALLOW_ALL_ORIGINS + CREDENTIALS=True 是严重安全漏洞（CVE级别）
    if parsed_cors_origins:
        CORS_ALLOWED_ORIGINS = parsed_cors_origins
        CORS_ALLOW_CREDENTIALS = True
    else:
        # 未配置 CORS_ALLOWED_ORIGINS 时，不允许任何跨域携带凭证的请求
        CORS_ALLOW_ALL_ORIGINS = False
        CORS_ALLOW_CREDENTIALS = False

    CORS_ALLOW_HEADERS = [
        'accept',
        'accept-encoding',
        'authorization',
        'content-type',
        'dnt',
        'origin',
        'user-agent',
        'x-csrftoken',
        'x-requested-with',
        'cache-control',  # 添加 SSE 需要的头部
    ]
    # SSE 需要的额外配置
    CORS_EXPOSE_HEADERS = ['Content-Type', 'Cache-Control']

# CSRF Settings
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='http://localhost:3000,http://127.0.0.1:3000',
    cast=parse_csv,
)

# Spectacular Settings
SPECTACULAR_SETTINGS = {
    'TITLE': 'TestHub API',
    'DESCRIPTION': 'Test Case Management Platform API',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

# 安全修复：Redis URL 不应硬编码密码
# 必须通过环境变量 REDIS_URL 设置
REDIS_URL = config('REDIS_URL', default='')
if not REDIS_URL:
    # 如果未设置 REDIS_URL，使用无密码的本地 Redis（仅限开发环境）
    if DEBUG:
        REDIS_URL = 'redis://127.0.0.1:6379/0'
        import warnings
        warnings.warn(
            "安全警告：REDIS_URL 未设置，使用默认无密码连接。"
            "生产环境必须设置 REDIS_URL 环境变量！",
            UserWarning
        )
    else:
        raise ValueError(
            "安全错误：REDIS_URL 未设置！\n"
            "生产环境必须在 .env 文件或环境变量中设置 REDIS_URL。\n"
            "格式：redis://:password@host:port/db"
        )

# Celery Configuration
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Channels Configuration
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [REDIS_URL],
        },
    },
}

# SMS Configuration (阿里云短信)
SMS_ACCESS_KEY_ID = config('SMS_ACCESS_KEY_ID', default='')
SMS_ACCESS_KEY_SECRET = config('SMS_ACCESS_KEY_SECRET', default='')
SMS_SIGN_NAME = config('SMS_SIGN_NAME', default='杭州智穹云启科技')
SMS_REGISTER_TEMPLATE_CODE = config('SMS_REGISTER_TEMPLATE_CODE', default='SMS_133001115')

# Cache Configuration
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
        'TIMEOUT': 300,  # 默认缓存超时5分钟
    }
}

# Email Configuration
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_USE_SSL = config('EMAIL_USE_SSL', default=False, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='webmaster@localhost')

# For 163 email with SSL, you might need this setting
EMAIL_TIMEOUT = 30

# 确保日志目录存在
log_dir = os.path.join(BASE_DIR, 'logs')
os.makedirs(log_dir, exist_ok=True)

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'logs', 'app.log'),
            'formatter': 'verbose',
        },
        'error_file': {
            'level': 'ERROR',
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'logs', 'error.log'),
            'formatter': 'verbose',
        },
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        # 其他具体模块的 logger 配置
        'django': {
            'handlers': ['file', 'error_file', 'console'],
            'level': 'INFO',
            'propagate': True,
        },
        'apps.data_factory.tools.json_tools': {
            'handlers': ['file', 'error_file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
        'apps.data_factory.tools.encoding_tools': {
            'handlers': ['file', 'error_file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
        'apps.data_factory.tools': {
            'handlers': ['file', 'error_file', 'console'],
            'level': 'INFO',
            'propagate': True,
        },
        'apps.analytics': {
            'handlers': ['file', 'error_file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
    'root': {
        'handlers': ['file', 'error_file', 'console'],
        'level': 'INFO',
        # 'propagate': True,
    },
}

# 指定simpleui默认的主题,指定一个文件名，相对路径就从simpleui的theme目录读取
SIMPLEUI_DEFAULT_THEME = 'admin.lte.css'
# 是否显示图标
SIMPLEUI_DEFAULT_ICON = True
# 是否关闭登录页粒子效果
SIMPLEUI_LOGIN_PARTICLES = True
# 后台管理首页，可以是url或者html文件
# SIMPLEUI_HOME_PAGE = 'https://www.baidu.com/'  # 后面可以扩展为大屏显示做统计
# 自定义首页标题
# SIMPLEUI_HOME_TITLE = 'Dashboard'
# # 自定义首页图标 首页图标,支持element-ui和fontawesome的图标，参考https://fontawesome.com/icons图标
# SIMPLEUI_HOME_ICON = 'fa fa-gauge'
# 设置simpleui 点击首页图标跳转的地址
SIMPLEUI_INDEX = 'http://localhost:3000'
# 自定义后台的Logo
SIMPLEUI_LOGO = 'https://static.djangoproject.com/img/favicon.6dbf28c0650e.ico'
# 是否显示首页信息
SIMPLEUI_HOME_INFO = False
# 是否显示快捷入口
SIMPLEUI_HOME_QUICK = True
# 是否显示最近动作
SIMPLEUI_HOME_ACTION = True
# 使用分析
SIMPLEUI_ANALYSIS = False
# 离线模式
SIMPLEUI_STATIC_OFFLINE = True
# True或None 默认显示加载遮罩层，指定为False 不显示遮罩层。默认显示
SIMPLEUI_LOADING = True
# 设置菜单icon，参考https://element.eleme.cn/#/zh-CN/component/icon
SIMPLEUI_ICON = {
    # 一级菜单项
    '测试执行管理': 'el-icon-s-tools',
    '用户管理': 'el-icon-user-solid',
    '令牌黑名单': 'el-icon-warning-outline',
    '智能助手': 'el-icon-chat-dot-round',
    '用例评审管理': 'el-icon-edit-outline',
    '认证令牌': 'el-icon-key',
    '认证和授权': 'el-icon-s-check',
    '需求分析': 'el-icon-notebook-2',

    # 二级菜单项
    '测试执行': 'el-icon-s-operation',
    '测试执行历史': 'el-icon-time',
    '测试执行用例': 'el-icon-document',
    '测试计划': 'el-icon-document-checked',
    '用户': 'el-icon-user',
    '用户配置': 'el-icon-setting',
    'Blacklisted Tokens': 'el-icon-warning-outline',
    'Outstanding Tokens': 'el-icon-s-custom',
    '任务执行日志': 'el-icon-s-data',
    '定时任务': 'el-icon-time',
    '测试套件': 'el-icon-suitcase',
    '环境变量': 'el-icon-school',
    '请求历史': 'el-icon-odometer',
    '智能助手会话': 'el-icon-chat-dot-round',
    '智能助手消息': 'el-icon-message',
    '测试用例评审': 'el-icon-check',
    '评审分配': 'el-icon-guide',
    '评审意见': 'el-icon-s-custom',
    '评审模板': 'el-icon-document',
    'Tokens': 'el-icon-key',
    '组': 'el-icon-s-custom',
    '业务需求': 'el-icon-document-checked',
    '分析任务': 'el-icon-stopwatch',
    '生成的测试用例': 'el-icon-document',
    '需求文档': 'el-icon-document',
}

# 开发环境，暂时禁用迁移历史检查
# SILENCED_SYSTEM_CHECKS = ['django.db.migrations.InconsistentMigrationHistory']
