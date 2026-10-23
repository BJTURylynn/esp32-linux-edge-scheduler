idf_component_register(SRCS "main.c" "frame.c"
    INCLUDE_DIRS "."
    REQUIRES esp_wifi esp_event esp_netif nvs_flash esp_timer lwip)
