<script setup lang="ts">
import { defineAsyncComponent } from "vue";
import type { PresentationBlock } from "./types";

defineProps<{ blocks: PresentationBlock[] }>();
const GenericChart = defineAsyncComponent(() => import("./GenericChart.vue"));
const displayValue = (value: unknown) => (
  typeof value === "object" ? JSON.stringify(value, null, 2) : String(value ?? "-")
);
</script>

<template>
  <section v-if="blocks.length" class="generic-response">
    <article v-for="(block, index) in blocks" :key="`${block.type}-${index}`" class="generic-block">
      <h4 v-if="block.title">{{ block.title }}</h4>
      <p v-if="block.type === 'markdown'" class="generic-markdown">{{ block.text }}</p>
      <dl v-else-if="block.type === 'key_value'" class="generic-key-values">
        <div v-for="(item, itemIndex) in block.items" :key="itemIndex">
          <dt>{{ item.label ?? item.key ?? `字段 ${itemIndex + 1}` }}</dt>
          <dd>{{ displayValue(item.value) }}</dd>
        </div>
      </dl>
      <div v-else-if="block.type === 'table'" class="generic-table-wrap">
        <table><thead><tr><th v-for="column in block.columns" :key="column">{{ column }}</th></tr></thead>
          <tbody><tr v-for="(row, rowIndex) in block.rows" :key="rowIndex"><td v-for="(value, valueIndex) in row" :key="valueIndex">{{ displayValue(value) }}</td></tr></tbody>
        </table>
      </div>
      <div v-else-if="block.type === 'metric'" class="generic-metrics">
        <div v-for="(item, itemIndex) in block.items" :key="itemIndex"><span>{{ item.label }}</span><strong>{{ displayValue(item.value) }}</strong></div>
      </div>
      <GenericChart v-else-if="block.type === 'chart'" :block="block" />
    </article>
  </section>
</template>
