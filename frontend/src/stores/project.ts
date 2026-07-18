import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as projectsApi from '../api/projects'
import type { Project, SearchItem } from '../types'

export const useProjectStore = defineStore('project', () => {
  const projects = ref<Project[]>([])
  const currentProjectId = ref<number | null>(null)
  const searchResults = ref<SearchItem[]>([])
  const loading = ref(false)

  async function load() {
    loading.value = true
    try {
      projects.value = await projectsApi.listProjects()
    } finally {
      loading.value = false
    }
    if (currentProjectId.value == null && projects.value.length) {
      currentProjectId.value = projects.value[0].id
    }
  }

  async function create(name: string): Promise<Project> {
    const p = await projectsApi.createProject(name)
    projects.value.unshift(p)
    currentProjectId.value = p.id
    return p
  }

  async function remove(id: number) {
    await projectsApi.deleteProject(id)
    projects.value = projects.value.filter((p) => p.id !== id)
    if (currentProjectId.value === id) {
      currentProjectId.value = projects.value[0]?.id ?? null
    }
  }

  async function search(q: string) {
    const term = q.trim()
    if (!term) {
      searchResults.value = []
      return
    }
    searchResults.value = await projectsApi.search(term)
  }

  return {
    projects,
    currentProjectId,
    searchResults,
    loading,
    load,
    create,
    remove,
    search,
  }
})
