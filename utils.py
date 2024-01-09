def get_dataset_details(id):
    dataset = {
        4: [31, "amazon", "/office-31/amazon/images"],
        5: [31, "webcam", "/office-31/webcam/images"],
        6: [31, "dslr", "/office-31/dslr/images"],
        7: [65, "artistic", "/officehome/artistic"],
        8: [65, "clip_art", "/officehome/clip_art"],
        9: [65, "product", "/officehome/product"],
        10: [65, "real_world", "/officehome/real_world"]
    }
    return dataset[id]