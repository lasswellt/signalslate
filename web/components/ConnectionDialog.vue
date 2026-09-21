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
import type { ConnectionCreate, ConnectionKind, ConnectionUpdate, ConnectionView } from '~/composables/useApi'

interface FieldDef {
  name: string
  label: string
  secret?: boolean
  // The immutable name that becomes part of the connection id; only asked for on create.
  isLabel?: boolean
  hint?: string
  options?: Array<{ label: string; value: string }>
}

// Field names, order and which ones are secret mirror api/routers/connections.py exactly. The gmail
// refresh token is not asked for here: the browser sign-in stores it.
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
      options: [
        { label: 'Paste back', value: 'paste_back' },
        { label: 'Callback', value: 'callback' },
      ],
    },
  ],
}

const KIND_OPTIONS = (['m365', 'zoom', 'slack', 'gmail'] as const).map((value) => ({
  label: value,
  value,
  attrs: { 'data-testid': `kind-${value}` },
}))

// Same rules as pipeline/connections.py (_LABEL_RULES, _IDENTIFIER and the secret bounds).
const LABEL_RULES: Partial<Record<ConnectionKind, { pattern: RegExp; max: number; text: string }>> = {
  m365: { pattern: /^[A-Za-z0-9-]+$/, max: 40, text: 'letters, digits and -' },
  slack: { pattern: /^[a-z0-9]+$/, max: 32, text: 'lowercase letters and digits' },
  gmail: { pattern: /^[a-z0-9]+$/, max: 32, text: 'lowercase letters and digits' },
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

const fields = computed(() => KIND_FIELDS[kind.value])
const visibleFields = computed(() => fields.value.filter((field) => !(isEdit.value && field.isLabel)))
const labelField = computed(() => fields.value.find((field) => field.isLabel)?.name)

function isSaved(name: string): boolean {
  return props.connection?.secrets_set.includes(name) ?? false
}

function defaultValues(forKind: ConnectionKind): Record<string, string> {
  return Object.fromEntries(
    KIND_FIELDS[forKind].filter((field) => !field.secret).map((field) => [field.name, field.options ? 'paste_back' : '']),
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

const providedSecrets = computed(() =>
  Object.entries(secrets.value).filter(([, value]) => value !== ''),
)

const hasChanges = computed(() => touched.value.size > 0 || providedSecrets.value.length > 0)

function buildCreate(): ConnectionCreate {
  const body: Record<string, string> = { kind: kind.value }
  for (const field of fields.value) {
    body[field.name] = field.secret ? (secrets.value[field.name] ?? '') : (values.value[field.name] ?? '')
  }
  // The body is assembled from KIND_FIELDS, which is the API contract, so it is exactly one member of
  // the ConnectionCreate union for the selected kind.
  return body as unknown as ConnectionCreate
}

function buildUpdate(): ConnectionUpdate {
  const body: { config?: Record<string, string>; secrets?: Record<string, string> } = {}
  const config = Object.fromEntries([...touched.value].map((name) => [name, values.value[name] ?? '']))
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
</script>
