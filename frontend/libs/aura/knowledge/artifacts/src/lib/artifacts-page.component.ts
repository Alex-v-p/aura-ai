import { ChangeDetectionStrategy, Component, ElementRef, HostListener, OnDestroy, ViewChild, computed, inject, signal } from '@angular/core'; import { A11yModule } from '@angular/cdk/a11y'; import { DatePipe } from '@angular/common'; import { ActivatedRoute, Router, RouterLink } from '@angular/router'; import { ArtifactsStore } from './artifacts-store'; import type { ActiveGeneratedArtifactDetail, GeneratedArtifactDetail, GeneratedArtifactMediaType } from '@aura/aura-api-client';
@Component({selector:'aura-artifacts-page',standalone:true,imports:[DatePipe,RouterLink,A11yModule],changeDetection:ChangeDetectionStrategy.OnPush,templateUrl:'./artifacts-page.component.html',styleUrl:'./artifacts-page.component.css'})
export class ArtifactsPageComponent implements OnDestroy {
  readonly store=inject(ArtifactsStore); private readonly route=inject(ActivatedRoute); private readonly router=inject(Router);
  readonly selectedId=signal<string|null>(null); readonly deleteOpen=signal(false); readonly deletionStatus=signal<string|null>(null);
  readonly typeFilter=signal<GeneratedArtifactMediaType|''>(''); readonly timeFilter=signal<'all'|'7d'|'30d'|'90d'>('all');
  readonly filteredItems=computed(()=>{const cutoff=this.cutoff();return this.store.items().filter((item)=>!cutoff||new Date(item.updatedAt).getTime()>=cutoff);});
  private deleteOrigin:HTMLButtonElement|null=null; private detailHeading?:ElementRef<HTMLElement>; private deletionStatusHeading?:ElementRef<HTMLElement>;
  @ViewChild('detailHeading') set detailHeadingView(value:ElementRef<HTMLElement>|undefined){this.detailHeading=value;if(value&&this.selectedId())this.focusDetailHeading();}
  @ViewChild('deletionStatusHeading') set deletionStatusView(value:ElementRef<HTMLElement>|undefined){this.deletionStatusHeading=value;if(value&&this.deletionStatus())this.focusDeletionStatus();}
  private readonly subscription=this.route.paramMap.subscribe(params=>{const id=params.get('id');this.selectedId.set(id);if(id){void this.store.loadDetail(id).then(()=>this.focusDetailHeading());}else{this.store.clearSelection();void this.store.load(this.typeFilter()||undefined);}});
  ngOnDestroy():void{this.subscription.unsubscribe();this.store.clearRouteState();}
  @HostListener('document:keydown.escape') onEscape():void{if(this.deleteOpen())this.closeDelete();}
  open(id:string):void{void this.router.navigate(['/artifacts',id]);} back():void{void this.router.navigate(['/artifacts']);}
  isActive(detail:GeneratedArtifactDetail):detail is ActiveGeneratedArtifactDetail{return detail.contentState==='active';}
  openDelete(event:Event):void{this.deleteOrigin=event.currentTarget as HTMLButtonElement;this.deleteOpen.set(true);}
  closeDelete():void{this.deleteOpen.set(false);queueMicrotask(()=>this.deleteOrigin?.focus());}
  async delete():Promise<void>{await this.store.delete();const detail=this.store.selected();this.closeDelete();this.deletionStatus.set(this.store.error()?'Artifact deletion failed; retained content is unchanged.':detail&&detail.contentState!=='active'?'Artifact deletion accepted; cleanup status is visible below.':'Artifact deletion outcome is unknown; reconcile before retrying.');this.focusDeletionStatus();}
  private focusDetailHeading(attempt=0):void{setTimeout(()=>{const heading=this.detailHeading?.nativeElement;if(heading&&document.contains(heading)){heading.focus();return;}if(attempt<5&&this.selectedId())this.focusDetailHeading(attempt+1);},attempt===0?0:16);}
  private focusDeletionStatus(attempt=0):void{setTimeout(()=>{const heading=this.deletionStatusHeading?.nativeElement;if(heading&&document.contains(heading)){heading.focus();return;}if(attempt<5&&this.deletionStatus())this.focusDeletionStatus(attempt+1);},attempt===0?0:16);}
  nextPage():void{const cursor=this.store.page().nextCursor;if(cursor)void this.store.load(this.typeFilter()||undefined,cursor);}
  setTypeFilter(value:string):void{const mediaType=value as GeneratedArtifactMediaType|'';this.typeFilter.set(mediaType);void this.store.load(mediaType||undefined);}
  setTimeFilter(value:string):void{if(value==='7d'||value==='30d'||value==='90d')this.timeFilter.set(value);else this.timeFilter.set('all');}
  private cutoff():number|null{const value=this.timeFilter();if(value==='all')return null;const days=Number.parseInt(value,10);return Date.now()-days*24*60*60*1000;}
  mediaLabel(media:string|null):string{return !media?'Content unavailable':media.startsWith('text/markdown')?'Markdown':media.startsWith('text/plain')?'Plain text':media==='application/json'?'JSON':'Content unavailable';}
}
