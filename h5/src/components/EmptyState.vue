<template><section class="empty-state"><div><q-icon :name="icon" size="52px" /></div><h2>{{ title }}</h2><p>{{ description }}</p></section></template>
<script setup lang="ts">defineProps<{ icon: string; title: string; description: string }>()</script>
