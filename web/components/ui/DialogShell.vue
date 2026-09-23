<script setup lang="ts">
import { computed, ref } from 'vue'
import { useQuasar } from 'quasar'

/**
 * Shared q-dialog chrome (DESIGN.md "Dialogs"): consistent header (title + close), scrollable body,
 * right-aligned action row, maximized below the `sm` breakpoint. Guards against losing unsaved work:
 * when `dirty`, any close attempt (close button, Esc, backdrop) asks for confirmation instead of
 * closing immediately.
 */
interface Props {
  modelValue: boolean
  title: string
  subtitle?: string
  busy?: boolean
  dirty?: boolean
  width?: string
  persistent?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  subtitle: undefined,
  busy: false,
  dirty: false,
  width: '560px',
  persistent: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  close: []
}>()

defineSlots<{
  default(): unknown
  actions(): unknown
}>()

const $q = useQuasar()
const maximized = computed(() => $q.screen.lt.sm)
const cardStyle = computed(() => (maximized.value ? undefined : { width: props.width, maxWidth: '100%' }))

const confirmOpen = ref(false)

/** Actually closes: notifies the parent and fires 'close'. Bypasses the dirty guard. */
function doClose() {
  emit('update:modelValue', false)
  emit('close')
}

/** Entry point for every dismiss attempt (close button, Esc, backdrop click). */
function requestClose() {
  if (props.busy) return
  if (props.dirty) {
    confirmOpen.value = true
    return
  }
  doClose()
}

// q-dialog only actually hides when its bound `model-value` prop changes (see useModelToggle):
// since we keep an `onUpdate:modelValue` listener, every dismiss attempt (Esc/backdrop/hide()) just
// calls this handler instead of closing outright, letting the dirty guard intercept it.
function onUpdateModelValue(value: boolean) {
  if (value) {
    emit('update:modelValue', true)
    return
  }
  requestClose()
}

function keepEditing() {
  confirmOpen.value = false
}

function confirmDiscard() {
  confirmOpen.value = false
  doClose()
}

// Lets a parent's own action buttons (e.g. a footer "Close") reuse the same dirty guard as the
// header close button, Esc, and backdrop instead of closing directly.
defineExpose({ requestClose })
</script>

<template>
  <q-dialog
    :model-value="modelValue"
    :maximized="maximized"
    :persistent="persistent"
    data-testid="dialog-shell"
    @update:model-value="onUpdateModelValue"
  >
    <q-card :style="cardStyle">
      <q-card-section class="row items-center no-wrap q-pb-none">
        <div class="col">
          <div class="text-h6" data-testid="dialog-title">{{ title }}</div>
          <div v-if="subtitle" class="text-caption text-grey-7" data-testid="dialog-subtitle">{{ subtitle }}</div>
        </div>
        <q-btn flat round dense icon="close" aria-label="Close" data-testid="dialog-close" :disable="busy" @click="requestClose">
          <q-tooltip>Close</q-tooltip>
        </q-btn>
      </q-card-section>

      <q-card-section class="scroll" style="max-height: 70vh">
        <slot />
      </q-card-section>

      <q-card-actions v-if="$slots.actions" align="right">
        <slot name="actions" />
      </q-card-actions>
    </q-card>
  </q-dialog>

  <q-dialog v-model="confirmOpen" data-testid="discard-confirm-dialog">
    <q-card>
      <q-card-section class="text-h6">Discard unsaved changes?</q-card-section>
      <q-card-actions align="right">
        <q-btn flat no-caps label="Keep editing" data-testid="keep-editing" @click="keepEditing" />
        <q-btn flat no-caps color="negative" label="Discard" data-testid="discard-confirm" @click="confirmDiscard" />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>
