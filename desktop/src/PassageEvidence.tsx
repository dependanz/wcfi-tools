export type SelectionEvidence={note:string;prior_excerpt_correction:string;
  mix_context?:{kit_below_mix_rms_db:number;note:string}};

export function PassageEvidence({evidence}:{evidence?:SelectionEvidence}){
  if(!evidence)return null;
  return <section className="study-analysis passage-evidence" role="note"><h3>Replacement section · sustained drum activity</h3>
    <p>{evidence.note}</p>
    <p className="review-small">{evidence.prior_excerpt_correction}</p>
    {evidence.mix_context&&<p><strong>Full-mix context:</strong> KIT RMS is {evidence.mix_context.kit_below_mix_rms_db.toFixed(1)} dB below the full mix.
      {' '}{evidence.mix_context.note}</p>}
  </section>;
}
