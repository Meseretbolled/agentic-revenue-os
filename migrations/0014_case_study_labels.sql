-- A case study is labelled either a public testimonial or vendor-reported (COOL's own figures about a customer).
ALTER TABLE case_studies DROP CONSTRAINT IF EXISTS case_studies_label_check;
ALTER TABLE case_studies ADD CONSTRAINT case_studies_label_check
    CHECK (label IN ('public testimonial', 'vendor-reported'));
