<template>
  <q-card data-testid="domain-ideas-panel">
    <q-card-section class="q-gutter-sm">
      <div class="text-h6">Domain ideas</div>

      <div>
        <div class="row items-center q-gutter-xs">
          <q-input
            v-model="seedInput"
            dense
            outlined
            label="Seed word"
            data-testid="ideas-seed-input"
            @keyup.enter="addSeed"
          />
          <q-btn flat dense no-caps label="Add" data-testid="ideas-seed-add" @click="addSeed" />
        </div>
        <div class="row items-center q-gutter-xs q-mt-xs" data-testid="ideas-seeds">
          <q-chip
            v-for="word in seeds"
            :key="word"
            removable
            :data-testid="`ideas-seed-${word}`"
            @remove="removeSeed(word)"
          >
            {{ word }}
          </q-chip>
        </div>
      </div>

      <div>
        <div class="row items-center q-gutter-xs">
          <q-input
            v-model="tldInput"
            dense
            outlined
            label="TLD"
            data-testid="ideas-tld-input"
            @keyup.enter="addTld"
          />
          <q-btn flat dense no-caps label="Add" data-testid="ideas-tld-add" @click="addTld" />
        </div>
        <div class="row items-center q-gutter-xs q-mt-xs" data-testid="ideas-tlds">
          <q-chip
            v-for="tld in tlds"
            :key="tld"
            removable
            :data-testid="`ideas-tld-${tld}`"
            @remove="removeTld(tld)"
          >
            .{{ tld }}
          </q-chip>
        </div>
      </div>

      <q-input
        v-model="brief"
        type="textarea"
        outlined
        dense
        label="Brief (optional, required to use Claude)"
        data-testid="ideas-brief"
      />
      <q-toggle v-model="useLlm" label="Use Claude to brainstorm additional names" data-testid="ideas-use-llm" />

      <div class="row items-center q-gutter-sm">
        <q-btn
          color="primary"
          no-caps
          label="Generate"
          :loading="generating"
          :disable="!canGenerate"
          data-testid="ideas-generate"
          @click="onGenerate"
        />
      </div>

      <q-banner v-if="llmReason" dense class="bg-grey-3" data-testid="ideas-llm-note">{{ llmReason }}</q-banner>
    </q-card-section>

    <q-separator />

    <q-card-section data-testid="ideas-results">
      <div v-if="generating" data-testid="ideas-generating">
        <q-skeleton type="text" width="60%" />
        <q-skeleton type="text" width="40%" />
        <q-skeleton type="text" width="50%" />
      </div>

      <div v-else-if="generateError" data-testid="ideas-generate-error">
        <q-banner dense class="bg-negative text-white">{{ generateError }}</q-banner>
        <q-btn flat no-caps label="Retry" data-testid="ideas-generate-retry" @click="onGenerate" />
      </div>

      <div v-else-if="!hasGenerated" class="text-grey-8" data-testid="ideas-hint">
        Add seed words and TLDs, then Generate to see candidate domain names.
      </div>

      <div v-else-if="candidates.length === 0" class="text-grey-8" data-testid="ideas-empty">
        No candidates found for these seeds and TLDs.
      </div>

      <template v-else>
        <div class="row items-center q-gutter-sm q-mb-sm">
          <q-select
            v-model="registrarConnectionId"
            :options="registrarOptions"
            emit-value
            map-options
            dense
            outlined
            clearable
            label="Registrar connection"
            style="min-width: 220px"
            data-testid="ideas-registrar-select"
          />
          <q-btn
            color="primary"
            no-caps
            label="Check with registrar"
            :loading="checking"
            :disable="selected.length === 0 || !registrarConnectionId"
            data-testid="ideas-check"
            @click="onCheck"
          />
          <span v-if="selectionLimitReached" class="text-grey-8" data-testid="ideas-select-limit">
            Up to {{ MAX_CHECK_NAMES }} domains can be checked at once.
          </span>
        </div>

        <q-banner v-if="notEligibleNote" dense class="bg-warning text-black" data-testid="ideas-not-eligible">
          {{ notEligibleNote }}
        </q-banner>
        <q-banner v-if="checkError" dense class="bg-negative text-white" data-testid="ideas-check-error">
          {{ checkError }}
        </q-banner>
        <q-banner v-if="connectionsError" dense class="bg-negative text-white" data-testid="ideas-connections-error">
          {{ connectionsError }}
        </q-banner>

        <div style="overflow-x: auto">
          <q-markup-table dense flat data-testid="ideas-results-table">
            <thead>
              <tr>
                <th></th>
                <th class="text-left">Name</th>
                <th class="text-left">Status</th>
                <th class="text-left">Registrar quote</th>
                <th class="text-left">Actions</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="candidate in candidates" :key="candidate.name">
                <td>
                  <q-checkbox
                    :model-value="isSelected(candidate.name)"
                    :disable="selectionLimitReached && !isSelected(candidate.name)"
                    :data-testid="`ideas-select-${candidate.name}`"
                    @update:model-value="(value: boolean) => toggleSelect(candidate.name, value)"
                  />
                </td>
                <td>{{ candidate.name }}</td>
                <td>
                  <q-badge :color="statusColor(candidate.status)" :data-testid="`ideas-status-${candidate.name}`">
                    {{ statusLabel(candidate.status) }}
                  </q-badge>
                </td>
                <td :data-testid="`ideas-quote-${candidate.name}`">
                  <template v-if="checkResults[candidate.name]">
                    {{ formatMoney(checkResults[candidate.name].price) }} {{ checkResults[candidate.name].currency }}
                    <q-badge v-if="checkResults[candidate.name].premium" color="warning" text-color="black">
                      Premium
                    </q-badge>
                  </template>
                  <span v-else class="text-grey-8">&mdash;</span>
                </td>
                <td>
                  <q-btn
                    flat
                    dense
                    no-caps
                    label="Watch"
                    :loading="watchingName === candidate.name"
                    :disable="watchedNames.has(candidate.name)"
                    :data-testid="`ideas-watch-${candidate.name}`"
                    @click="onWatch(candidate.name)"
                  />
                  <span v-if="watchedNames.has(candidate.name)" class="text-positive q-ml-xs" :data-testid="`ideas-watched-${candidate.name}`">
                    Watched
                  </span>
                  <span v-if="watchErrors[candidate.name]" class="text-negative q-ml-xs" :data-testid="`ideas-watch-error-${candidate.name}`">
                    {{ watchErrors[candidate.name] }}
                  </span>
                  <q-btn
                    flat
                    dense
                    no-caps
                    color="primary"
                    label="Buy"
                    :disable="!registrarConnectionId"
                    :data-testid="`ideas-buy-${candidate.name}`"
                    @click="onBuyClick(candidate.name)"
                  />
                </td>
              </tr>
            </tbody>
          </q-markup-table>
        </div>
      </template>
    </q-card-section>

    <DomainPurchaseDialog
      v-if="buyOpen"
      :open="buyOpen"
      :name="buyName"
      :connection-id="buyConnectionId"
      @update:open="buyOpen = $event"
      @closed="buyOpen = false"
    />
  </q-card>
</template>

<script setup lang="ts">
import { ApiError } from '~/composables/useApi'
import { useApi } from '~/composables/useApi'
import type { ConnectionView } from '~/composables/useApi'
import { useDomainsApi } from '~/composables/useDomainsApi'
import type { CandidateOut, QuoteOut } from '~/composables/useDomainsApi'

// api/routers/domain_buy.py CheckBody.names: list[str] = Field(min_length=1, max_length=50).
const MAX_CHECK_NAMES = 50

const api = useApi()
const domainsApi = useDomainsApi()

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong'
}

function formatMoney(value: string | number): string {
  const amount = Number(value)
  return Number.isFinite(amount) ? amount.toFixed(2) : String(value)
}

// Seed words
const seedInput = ref('')
const seeds = ref<string[]>([])

function addSeed() {
  const value = seedInput.value.trim().toLowerCase()
  if (value && !seeds.value.includes(value)) seeds.value = [...seeds.value, value]
  seedInput.value = ''
}

function removeSeed(word: string) {
  seeds.value = seeds.value.filter((w) => w !== word)
}

// TLDs
const tldInput = ref('')
const tlds = ref<string[]>([])

function addTld() {
  const value = tldInput.value.trim().toLowerCase().replace(/^\.+/, '')
  if (value && !tlds.value.includes(value)) tlds.value = [...tlds.value, value]
  tldInput.value = ''
}

function removeTld(tld: string) {
  tlds.value = tlds.value.filter((t) => t !== tld)
}

// Brief + LLM toggle
const brief = ref('')
const useLlm = ref(false)

// Generate
const generating = ref(false)
const generateError = ref<string | null>(null)
const llmReason = ref<string | null>(null)
const candidates = ref<CandidateOut[]>([])
const hasGenerated = ref(false)

const canGenerate = computed(() => seeds.value.length > 0 && tlds.value.length > 0 && !generating.value)

/** Calls POST /api/domains/ideas with the current seeds/TLDs/brief/use_llm and resets any prior
 * selection and registrar-check results, since they belonged to the previous candidate list. */
async function onGenerate() {
  if (!canGenerate.value) return
  generating.value = true
  generateError.value = null
  try {
    const result = await domainsApi.generateIdeas({
      seeds: seeds.value,
      tlds: tlds.value,
      brief: brief.value.trim() || undefined,
      use_llm: useLlm.value,
    })
    candidates.value = result.candidates
    llmReason.value = result.llm_reason
    hasGenerated.value = true
    selected.value = []
    checkResults.value = {}
    checkError.value = null
    notEligibleNote.value = null
  } catch (err) {
    generateError.value = errorMessage(err)
  } finally {
    generating.value = false
  }
}

// Selection
const selected = ref<string[]>([])
const selectionLimitReached = computed(() => selected.value.length >= MAX_CHECK_NAMES)

function isSelected(name: string): boolean {
  return selected.value.includes(name)
}

function toggleSelect(name: string, checked: boolean) {
  if (checked) {
    if (selected.value.includes(name) || selected.value.length >= MAX_CHECK_NAMES) return
    selected.value = [...selected.value, name]
  } else {
    selected.value = selected.value.filter((n) => n !== name)
  }
}

function statusLabel(status: string): string {
  if (status === 'likely_available') return 'Likely available'
  if (status === 'taken') return 'Taken'
  return 'Unknown'
}

function statusColor(status: string): string {
  if (status === 'likely_available') return 'positive'
  if (status === 'taken') return 'negative'
  return 'grey-6'
}

// Registrar connections (namecheap/godaddy support check(); wordpress raises Unsupported, so it is
// left out of this picker).
const connections = ref<ConnectionView[]>([])
const connectionsError = ref<string | null>(null)
const registrarConnectionId = ref<string | null>(null)

const registrarOptions = computed(() =>
  connections.value
    .filter((c) => c.kind === 'namecheap' || c.kind === 'godaddy')
    .map((c) => ({ label: `${c.label} (${c.kind})`, value: c.id })),
)

async function loadConnections() {
  connectionsError.value = null
  try {
    connections.value = await api.listConnections()
  } catch (err) {
    connectionsError.value = errorMessage(err)
  }
}

onMounted(() => {
  void loadConnections()
})

// Registrar check
const checking = ref(false)
const checkError = ref<string | null>(null)
const notEligibleNote = ref<string | null>(null)
const checkResults = ref<Record<string, QuoteOut>>({})

/** Authoritative check via POST /api/domains/check. The whole call fails with a single 409
 * not_eligible when the registrar rejects the account for this operation (e.g. GoDaddy's minimum
 * account size for its availability API): that is shown as a readable note, not per-row. */
async function onCheck() {
  if (!registrarConnectionId.value || selected.value.length === 0) return
  checking.value = true
  checkError.value = null
  notEligibleNote.value = null
  try {
    const quotes = await domainsApi.checkDomains({
      names: [...selected.value],
      connection_id: registrarConnectionId.value,
    })
    const next = { ...checkResults.value }
    for (const quote of quotes) next[quote.name] = quote
    checkResults.value = next
  } catch (err) {
    if (err instanceof ApiError && err.code === 'not_eligible') notEligibleNote.value = err.message
    else checkError.value = errorMessage(err)
  } finally {
    checking.value = false
  }
}

// Watch
const watchingName = ref<string | null>(null)
const watchedNames = ref<Set<string>>(new Set())
const watchErrors = ref<Record<string, string>>({})

async function onWatch(name: string) {
  watchingName.value = name
  const nextErrors = { ...watchErrors.value }
  delete nextErrors[name]
  watchErrors.value = nextErrors
  try {
    await domainsApi.addDomain({ name, ownership: 'watched' })
    watchedNames.value = new Set(watchedNames.value).add(name)
  } catch (err) {
    watchErrors.value = { ...watchErrors.value, [name]: errorMessage(err) }
  } finally {
    watchingName.value = null
  }
}

// Buy
const buyOpen = ref(false)
const buyName = ref('')
const buyConnectionId = ref('')

function onBuyClick(name: string) {
  if (!registrarConnectionId.value) return
  buyName.value = name
  buyConnectionId.value = registrarConnectionId.value
  buyOpen.value = true
}
</script>
