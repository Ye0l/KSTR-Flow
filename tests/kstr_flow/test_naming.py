from kstr_flow.naming import str_to_class_id, str_to_raw_id


def test_identifier_normalization():
    assert str_to_raw_id("Face Detailer") == "Face_Detailer"
    assert str_to_raw_id("123 node") == "_123_node"
    assert str_to_raw_id("class") == "class_"
    assert str_to_class_id("Face Detailer") == "FaceDetailer"
