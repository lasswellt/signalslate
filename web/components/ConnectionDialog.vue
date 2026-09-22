<template>
  <!-- persistent: a click outside or Esc must not throw away what was typed (and a half-typed secret
       is not recoverable from the form afterwards). Closing is Cancel or a saved connection. -->
  <q-dialog persistent :model-value="modelValue" @update:model-value="(value: boolean) => emit('update:modelValue', value)">
    <q-card style="min-width: 360px; max-width: 520px; width: 100%" data-testid="connection-dialog">
      <q-form greedy autocomplete="off" @submit="onSubmit">
        <q-card-section>
          <div class="text-h6" data-testid="dialog-title">{{ isEdit ? `Edit ${connection?.id}` : 'Add connection' }}</div>
        </q-card-section>

        <q-card-section class="q-gutter-md">
          <q-banner
            v-if="isEdit && connection?.origin === 'env'"
            dense
            class="bg-grey-3"
            data-testid="env-note"
          >
            This connection was seeded from .env. Saving here stores the change in the UI store, which is
            authoritative from now on: the .env value is no longer read for it.
          </q-banner>

          <q-btn-toggle
            v-if="!isEdit"
            v-model="kind"
            no-caps
            unelevated
            toggle-color="primary"
            :options="KIND_OPTIONS"
            :disable="busy"
            data-testid="kind-selector"
          />

          <template v-if="kind === 'zoom'">
            <q-btn-toggle
              :model-value="zoomAuthMode"
              no-caps
              unelevated
              toggle-color="primary"
              :options="ZOOM_AUTH_MODE_OPTIONS"
              :disable="busy"
              data-testid="zoom-auth-mode"
              @update:model-value="onZoomAuthModeInput"
            />

            <template v-if="zoomAuthMode === 'oauth'">
              <div class="text-body2" data-testid="zoom-redirect-uri">
                Add this as the OAuth redirect URL (and to the allow list) in your Zoom General app,
                replacing &lt;PUBLIC_BASE_URL&gt; with this app's public https address:
                &lt;PUBLIC_BASE_URL&gt;/api/oauth/callback/zoom
              </div>
              <q-banner
                v-if="systemInfo && !systemInfo.public_base_url_configured"
                dense
                class="bg-warning text-black"
                data-testid="zoom-callback-missing"
              >
                Zoom sign-in needs an https PUBLIC_BASE_URL configured for this app.
              </q-banner>
            </template>
          </template>

          <template v-for="field in visibleFields" :key="`${kind}-${field.name}`">
            <q-select
              v-if="field.options"
              :model-value="values[field.name]"
              :options="field.options"
              :label="field.label"
              outlined
              dense
              emit-value
              map-options
              :disable="busy"
              :error="!!fieldErrors[field.name]"
              :error-message="fieldErrors[field.name]"
              :data-testid="`field-${field.name}`"
              @update:model-value="(value: string) => onConfigInput(field.name, value)"
            />

            <div v-else-if="field.secret && isEdit && !replacing[field.name] && isSaved(field.name)" class="row items-center q-gutter-sm">
              <div class="text-body2">{{ field.label }}</div>
              <q-chip dense color="positive" text-color="white" data-testid="saved-chip">Saved</q-chip>
              <q-btn
                flat
                dense
                no-caps
                label="Replace"
                :disable="busy"
                :data-testid="`replace-${field.name}`"
                @click="replacing[field.name] = true"
              />
            </div>

            <q-input
              v-else-if="field.secret && field.textarea"
              type="textarea"
              autogrow
              :model-value="secrets[field.name] ?? ''"
              :label="isEdit ? `New ${field.label}` : field.label"
              :hint="field.hint"
              outlined
              dense
              :disable="busy"
              :rules="[(value: string) => checkSecretJson(field, value)]"
              :error="!!fieldErrors[field.name]"
              :error-message="fieldErrors[field.name]"
              :data-testid="`field-${field.name}`"
              @update:model-value="(value: string | number | null) => onSecretInput(field.name, value)"
            >
              <template v-if="isEdit && isSaved(field.name)" #append>
                <q-btn flat dense no-caps label="Keep saved" :data-testid="`keep-${field.name}`" @click="keepSaved(field.name)" />
              </template>
            </q-input>

            <q-input
              v-else-if="field.secret"
              type="password"
              autocomplete="new-password"
              :model-value="secrets[field.name] ?? ''"
              :label="isEdit ? `New ${field.label}` : field.label"
              outlined
              dense
              :disable="busy"
              :rules="[(value: string) => checkSecret(field, value)]"
              :error="!!fieldErrors[field.name]"
              :error-message="fieldErrors[field.name]"
              :data-testid="`field-${field.name}`"
              @update:model-value="(value: string | number | null) => onSecretInput(field.name, value)"
            >
              <template v-if="isEdit && isSaved(field.name)" #append>
                <q-btn flat dense no-caps label="Keep saved" :data-testid="`keep-${field.name}`" @click="keepSaved(field.name)" />
              </template>
            </q-input>

            <q-input
              v-else
              :model-value="values[field.name]"
              :label="field.label"
              :hint="field.hint"
              outlined
              dense
              autocomplete="off"
              :disable="busy"
              :rules="[(value: string) => checkConfig(field, value)]"
              :error="!!fieldErrors[field.name]"
              :error-message="fieldErrors[field.name]"
              :data-testid="`field-${field.name}`"
              @update:model-value="(value: string | number | null) => onConfigInput(field.name, String(value ?? ''))"
            />
          </template>

          <template v-if="kind === 'zoom'">
            <q-toggle
              :model-value="zoomIncludeTranscripts"
              label="Include transcripts"
              :disable="busy"
              data-testid="zoom-include-transcripts"
              @update:model-value="onZoomTranscriptsInput"
            />
            <div class="text-caption text-grey-7">
              Needs Zoom cloud recording; meetings without a recording simply have no transcript.
            </div>
          </template>

          <q-banner v-if="formError" dense class="bg-negative text-white" data-testid="form-error">
            {{ formError }}
          </q-banner>
        </q-card-section>

        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" :disable="busy" data-testid="dialog-cancel" @click="close" />
          <q-btn
            type="submit"
            color="primary"
            no-caps
            :label="isEdit ? 'Save' : 'Add'"
            :loading="busy"
            :disable="isEdit && !hasChanges"
            data-testid="dialog-submit"
          />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { ApiError } from '~/composables/useApi'
import type { ConnectionCreate, ConnectionKind, ConnectionUpdate, ConnectionView, SystemInfo } from '~/composables/useApi'

interface FieldDef {
  name: string
  label: string
  secret?: boolean
  // A secret field rendered as a textarea (registrant_contact: a JSON object, not a token).
  textarea?: boolean
  // Not in the kind's required_secrets/required config (pipeline/connections.py KINDS): may be left blank on create.
  optional?: boolean
  // The immutable name that becomes part of the connection id; only asked for on create.
  isLabel?: boolean
  hint?: string
  options?: Array<{ label: string; value: string }>
  // The value defaultValues() fills in for a select field; '' for a plain text field.
  default?: string
}

// Field names, order and which ones are secret mirror api/routers/connections.py exactly. The gmail
// refresh token and the wordpress access token are not asked for here: browser sign-in stores them.
const KIND_FIELDS: Record<ConnectionKind, FieldDef[]> = {
  m365: [
    { name: 'alias', label: 'Alias', isLabel: true, hint: 'Letters, digits and - (max 40)' },
    { name: 'tenant_id', label: 'Tenant ID' },
    { name: 'client_id', label: 'Client ID' },
  ],
  zoom: [
    { name: 'account_id', label: 'Account ID' },
    { name: 'client_id', label: 'Client ID' },
    { name: 'client_secret', label: 'Client secret', secret: true },
  ],
  slack: [
    { name: 'label', label: 'Label', isLabel: true, hint: 'Lowercase letters and digits (max 32)' },
    { name: 'token', label: 'Token', secret: true },
  ],
  gmail: [
    { name: 'label', label: 'Label', isLabel: true, hint: 'Lowercase letters and digits (max 32)' },
    { name: 'client_id', label: 'Client ID' },
    { name: 'client_secret', label: 'Client secret', secret: true },
    {
      name: 'redirect_mode',
      label: 'Redirect mode',
      default: 'paste_back',
      options: [
        { label: 'Paste back', value: 'paste_back' },
        { label: 'Callback', value: 'callback' },
      ],
    },
  ],
  namecheap: [
    { name: 'label', label: 'Label', isLabel: true, hint: 'Lowercase letters and digits (max 32)' },
    { name: 'api_user', label: 'API user' },
    { name: 'username', label: 'Username' },
    { name: 'client_ip', label: 'Client IP', hint: 'public IPv4 of this server' },
    { name: 'api_key', label: 'API key', secret: true },
    {
      name: 'sandbox',
      label: 'Sandbox',
      default: 'false',
      options: [
        { label: 'Production', value: 'false' },
        { label: 'Sandbox', value: 'true' },
      ],
    },
    { name: 'registrant_contact', label: 'Registrant contact', secret: true, textarea: true, optional: true, hint: 'JSON object' },
  ],
  godaddy: [
    { name: 'label', label: 'Label', isLabel: true, hint: 'Lowercase letters and digits (max 32)' },
    { name: 'api_key', label: 'API key', secret: true },
    { name: 'api_secret', label: 'API secret', secret: true },
    {
      name: 'environment',
      label: 'Environment',
      default: 'production',
      options: [
        { label: 'Production', value: 'production' },
        { label: 'OTE', value: 'ote' },
      ],
    },
    { name: 'registrant_contact', label: 'Registrant contact', secret: true, textarea: true, optional: true, hint: 'JSON object' },
  ],
  wordpress: [
    { name: 'label', label: 'Label', isLabel: true, hint: 'Lowercase letters and digits (max 32)' },
    { name: 'client_id', label: 'Client ID' },
    { name: 'client_secret', label: 'Client secret', secret: true },
    {
      name: 'redirect_mode',
      label: 'Redirect mode',
      default: 'paste_back',
      options: [
        { label: 'Paste back', value: 'paste_back' },
        { label: 'Callback', value: 'callback' },
      ],
    },
  ],
}

const KIND_OPTIONS = (['m365', 'zoom', 'slack', 'gmail', 'namecheap', 'godaddy', 'wordpress'] as const).map((value) => ({
  label: value,
  value,
  attrs: { 'data-testid': `kind-${value}` },
}))

// zoom's auth_mode: "oauth" (Sign in with Zoom, the default) or "s2s" (Server-to-Server). Same rule
// as pipeline.connections.zoom_auth_mode: an explicit auth_mode wins; otherwise "s2s" when
// account_id is set and "oauth" otherwise.
const ZOOM_AUTH_MODE_OPTIONS = [
  { label: 'Sign in with Zoom', value: 'oauth', attrs: { 'data-testid': 'zoom-auth-oauth' } },
  { label: 'Server-to-Server', value: 's2s', attrs: { 'data-testid': 'zoom-auth-s2s' } },
]

// Same rules as pipeline/connections.py (_LABEL_RULES, _IDENTIFIER and the secret bounds).
const LABEL_RULES: Partial<Record<ConnectionKind, { pattern: RegExp; max: number; text: string }>> = {
  m365: { pattern: /^[A-Za-z0-9-]+$/, max: 40, text: 'letters, digits and -' },
  slack: { pattern: /^[a-z0-9]+$/, max: 32, text: 'lowercase letters and digits' },
  gmail: { pattern: /^[a-z0-9]+$/, max: 32, text: 'lowercase letters and digits' },
  namecheap: { pattern: /^[a-z0-9]+$/, max: 32, text: 'lowercase letters and digits' },
  godaddy: { pattern: /^[a-z0-9]+$/, max: 32, text: 'lowercase letters and digits' },
  wordpress: { pattern: /^[a-z0-9]+$/, max: 32, text: 'lowercase letters and digits' },
}
const IDENTIFIER = /^[A-Za-z0-9._~@:-]+$/
const IDENTIFIER_MAX = 128
const SECRET_MAX = 4096
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\x00-\x1f\x7f]/

const props = defineProps<{
  modelValue: boolean
  mode: 'create' | 'edit'
  connection?: ConnectionView | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: [connection: ConnectionView, mode: 'create' | 'edit']
}>()

const api = useApi()

const isEdit = computed(() => props.mode === 'edit')
const kind = ref<ConnectionKind>('slack')
const values = ref<Record<string, string>>({})
// Secrets live only here: cleared in the finally of every request and whenever the dialog closes.
const secrets = ref<Record<string, string>>({})
const replacing = ref<Record<string, boolean>>({})
const touched = ref<Set<string>>(new Set())
const fieldErrors = ref<Record<string, string>>({})
const formError = ref<string | null>(null)
const busy = ref(false)

// zoom-only: auth_mode and include_transcripts are not free-text config fields, so they live
// outside `values`/`touched` and are merged into the submitted body explicitly.
const zoomAuthMode = ref<'oauth' | 's2s'>('oauth')
const zoomIncludeTranscripts = ref(true)
const zoomAuthModeTouched = ref(false)
const zoomTranscriptsTouched = ref(false)
// The API deliberately never returns the public URL itself (SystemInfo.public_base_url_configured
// is a bool); fetched once so the redirect-URL note can warn when it is not set.
const systemInfo = ref<SystemInfo | null>(null)

const fields = computed(() => KIND_FIELDS[kind.value])
const visibleFields = computed(() =>
  fields.value.filter((field) => {
    if (isEdit.value && field.isLabel) return false
    if (kind.value === 'zoom' && field.name === 'account_id' && zoomAuthMode.value === 'oauth') return false
    return true
  }),
)
const labelField = computed(() => fields.value.find((field) => field.isLabel)?.name)

function isSaved(name: string): boolean {
  return props.connection?.secrets_set.includes(name) ?? false
}

function defaultValues(forKind: ConnectionKind): Record<string, string> {
  return Object.fromEntries(
    KIND_FIELDS[forKind].filter((field) => !field.secret).map((field) => [field.name, field.default ?? '']),
  )
}

function reset(forKind: ConnectionKind) {
  kind.value = forKind
  secrets.value = {}
  replacing.value = {}
  touched.value = new Set()
  fieldErrors.value = {}
  formError.value = null
  const initial = defaultValues(forKind)
  if (isEdit.value && props.connection) {
    for (const name of Object.keys(initial)) initial[name] = props.connection.config[name] ?? initial[name] ?? ''
  }
  values.value = initial
  if (forKind === 'zoom') {
    zoomAuthModeTouched.value = false
    zoomTranscriptsTouched.value = false
    const config: Record<string, string> = isEdit.value && props.connection ? props.connection.config : {}
    zoomAuthMode.value =
      config.auth_mode === 'oauth' || config.auth_mode === 's2s' ? config.auth_mode : config.account_id ? 's2s' : 'oauth'
    zoomIncludeTranscripts.value = config.include_transcripts !== 'false'
  }
}

function clearSecrets() {
  secrets.value = {}
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) reset(isEdit.value && props.connection ? props.connection.kind : 'slack')
    else clearSecrets()
  },
  { immediate: true },
)

watch(kind, (next, previous) => {
  // Switching kind on the create form starts a fresh form; a secret typed for another kind must go.
  if (next !== previous && !isEdit.value) reset(next)
})

function onConfigInput(name: string, value: string) {
  values.value[name] = value
  touched.value.add(name)
  delete fieldErrors.value[name]
}

function onSecretInput(name: string, value: string | number | null) {
  secrets.value[name] = String(value ?? '')
  delete fieldErrors.value[name]
}

function keepSaved(name: string) {
  delete secrets.value[name]
  replacing.value[name] = false
}

function onZoomAuthModeInput(value: string) {
  zoomAuthMode.value = value as 'oauth' | 's2s'
  zoomAuthModeTouched.value = true
}

function onZoomTranscriptsInput(value: boolean) {
  zoomIncludeTranscripts.value = value
  zoomTranscriptsTouched.value = true
}

function checkConfig(field: FieldDef, value: string): true | string {
  // An edit only sends touched fields, so an untouched prefilled value is not judged.
  if (isEdit.value && !touched.value.has(field.name)) return true
  if (!value) return 'Required'
  if (field.isLabel) {
    const rule = LABEL_RULES[kind.value]
    if (rule && (value.length > rule.max || !rule.pattern.test(value))) {
      return `Use ${rule.text}, at most ${rule.max} characters`
    }
    return true
  }
  if (value.length > IDENTIFIER_MAX || !IDENTIFIER.test(value)) {
    return `1 to ${IDENTIFIER_MAX} characters of letters, digits and . _ ~ @ : -`
  }
  return true
}

function checkSecret(field: FieldDef, value: string): true | string {
  if (!value) return isEdit.value ? true : 'Required'
  if (!value.trim()) return `${field.label} must not be blank`
  if (value.length > SECRET_MAX || CONTROL_CHARS.test(value)) {
    return `At most ${SECRET_MAX} characters with no control characters`
  }
  return true
}

// registrant_contact: same JSON-object shape pipeline.connections._validate_registrant_contact
// requires server-side (docs/plans/domain-collector); checked here only to fail fast, not to
// replace that validation.
function checkSecretJson(field: FieldDef, value: string): true | string {
  if (!value) return isEdit.value || field.optional ? true : 'Required'
  if (value.length > SECRET_MAX || CONTROL_CHARS.test(value)) {
    return `At most ${SECRET_MAX} characters with no control characters`
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(value)
  } catch {
    return `${field.label} must be valid JSON`
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return `${field.label} must be a JSON object`
  }
  return true
}

const providedSecrets = computed(() =>
  Object.entries(secrets.value).filter(([, value]) => value !== ''),
)

const hasChanges = computed(
  () =>
    touched.value.size > 0 ||
    providedSecrets.value.length > 0 ||
    (kind.value === 'zoom' && (zoomAuthModeTouched.value || zoomTranscriptsTouched.value)),
)

function buildCreate(): ConnectionCreate {
  const body: Record<string, unknown> = { kind: kind.value }
  for (const field of fields.value) {
    // account_id is part of the zoom contract but only makes sense in s2s mode.
    if (kind.value === 'zoom' && field.name === 'account_id' && zoomAuthMode.value === 'oauth') continue
    const value = field.secret ? (secrets.value[field.name] ?? '') : (values.value[field.name] ?? '')
    // An optional secret left blank (registrant_contact) must be omitted, not sent as '': the server
    // validates whatever it receives, and '' does not parse as the JSON object the field expects.
    if (field.optional && value === '') continue
    body[field.name] = value
  }
  if (kind.value === 'zoom') {
    // ZoomCreate.include_transcripts is a JSON bool (api/routers/connections.py); auth_mode is always sent.
    body.auth_mode = zoomAuthMode.value
    body.include_transcripts = zoomIncludeTranscripts.value
  }
  // The body is assembled from KIND_FIELDS, which is the API contract, so it is exactly one member of
  // the ConnectionCreate union for the selected kind.
  return body as unknown as ConnectionCreate
}

function buildUpdate(): ConnectionUpdate {
  const body: { config?: Record<string, string>; secrets?: Record<string, string> } = {}
  const config = Object.fromEntries(
    [...touched.value]
      .filter((name) => !(kind.value === 'zoom' && name === 'account_id' && zoomAuthMode.value === 'oauth'))
      .map((name) => [name, values.value[name] ?? '']),
  )
  if (kind.value === 'zoom') {
    // pipeline.connections stores every config value as a string, unlike the create body's JSON bool.
    if (zoomAuthModeTouched.value) config.auth_mode = zoomAuthMode.value
    if (zoomAuthModeTouched.value && zoomAuthMode.value === 's2s') config.account_id = values.value.account_id ?? ''
    if (zoomTranscriptsTouched.value) config.include_transcripts = zoomIncludeTranscripts.value ? 'true' : 'false'
  }
  if (Object.keys(config).length) body.config = config
  if (providedSecrets.value.length) body.secrets = Object.fromEntries(providedSecrets.value)
  return body
}

const FIELD_ERROR = /^([A-Za-z_][A-Za-z0-9_.]*): (.+)$/

/**
 * Puts an API failure on the field it names. useApi flattens a 422 to `loc: msg` entries joined by
 * "; " (loc may carry a union-tag prefix, so the last segment is the field name); anything that does
 * not name one of this form's fields goes in the form banner. Only the API's own text is shown.
 */
function showError(error: unknown) {
  if (!(error instanceof ApiError)) {
    formError.value = 'Something went wrong'
    return
  }
  const known = new Set(fields.value.map((field) => field.name))
  if (error.status === 422) {
    const unmapped: string[] = []
    for (const entry of error.message.split('; ')) {
      const match = FIELD_ERROR.exec(entry)
      const name = match?.[1]?.split('.').pop()
      if (match && name && known.has(name)) fieldErrors.value[name] = match[2] ?? ''
      else unmapped.push(entry)
    }
    formError.value = unmapped.length ? unmapped.join('; ') : null
    return
  }
  if (error.status === 409 && labelField.value) {
    fieldErrors.value[labelField.value] = error.message
    return
  }
  formError.value = error.message
}

async function onSubmit() {
  if (busy.value) return
  busy.value = true
  fieldErrors.value = {}
  formError.value = null
  try {
    const saved = isEdit.value && props.connection
      ? await api.updateConnection(props.connection.id, buildUpdate())
      : await api.createConnection(buildCreate())
    emit('saved', saved, props.mode)
    emit('update:modelValue', false)
  } catch (error) {
    showError(error)
  } finally {
    clearSecrets()
    busy.value = false
  }
}

function close() {
  clearSecrets()
  emit('update:modelValue', false)
}

onMounted(async () => {
  try {
    systemInfo.value = await api.getSystem()
  } catch {
    systemInfo.value = null
  }
})
</script>
